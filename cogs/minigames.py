"""Channel games the members play and the bot only keeps honest.

Counting and word chain are not really *bot* games: people post in a channel and
the fun is in the streak. What a bot adds is the one thing a human moderator
cannot do at three in the morning — notice the wrong message immediately, remove
it, and tell only its author why.

Everything here therefore runs in `on_message`, which is the hottest path in the
project. Two consequences shape the whole module. The channel check has to be a
cheap in-memory read before anything else happens, and a failure must never
raise out of the listener, because an exception there is a listener that stops
running for every guild.

Rules that bind changes here: docs/subsystems/minigames.md
"""

import logging
import os
import re
import sys
import unicodedata

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import discord
from discord.ext import commands

from core import database
from core import wordchain_dictionary
from cogs.utils import guild_setting_sync, guild_settings_sync, t
from core.feature_access import is_enabled, maintenance_blocks

minigame_logger = logging.getLogger("PotatoBot.Minigames")

# A counting message is a number and nothing else. Anything with a word in it is
# chatter, and chatter is left alone rather than deleted — a channel where an
# aside gets removed is a channel nobody talks in.
COUNT_PATTERN = re.compile(r"^\s*(\d{1,9})\s*$")
# A word-chain entry is one word: letters only, so a link or a sentence is
# chatter by the same rule.
WORD_PATTERN = re.compile(r"^\s*([^\W\d_]{2,32})\s*$", re.UNICODE)

# How often a turn is marked. See the comment where it is used: the chain here
# never breaks, so the milestone is a round number rather than a new record.
MILESTONE_EVERY = 100

# Which setting names each game's channel, and which flag owns it.
GAMES = {
    "counting": {"channel": "counting_channel", "feature": "minigame_counting"},
    "word_chain": {"channel": "word_chain_channel",
                   "feature": "minigame_word_chain"},
}


MINIGAME_CHOICES = [
    discord.app_commands.Choice(name=t(f"dashboard.features.minigame_{key}"),
                                value=key)
    for key in GAMES
]


# Nine Hungarian letters are written with two characters and one with three,
# and a chain that does not know them argues with every member who plays one:
# `busz` ends in `sz`, so the next word is `szek` and not `zebra`. The order is
# longest match first, because `dzs` contains both `dz` and `zs` — `bridzs`
# ends in `dzs`, and a two-character match would read it as `zs`.
#
# Doubled digraphs need nothing extra. Hungarian doubles the *first* character
# (`ssz`, `ggy`, `nny`, `ccs`), so the last two characters of `rossz` already
# spell `sz`.
HUNGARIAN_LETTERS = ("dzs", "cs", "dz", "gy", "ly", "ny", "sz", "ty", "zs")

# Declared per language rather than applied to everyone. `only` and `many` end
# in ordinary English letters, so a digraph alphabet in an English guild would
# refuse every word after them that does not begin `ly` or `ny`. A language
# with no entry here plays by single letters, which is what every chain did
# before this existed.
LETTER_GROUPS = {"hu": HUNGARIAN_LETTERS}


def chain_letters() -> tuple[str, ...]:
    """The multi-character letters the configured language writes.

    Resolved per message rather than at import: `MINIGAME_CHOICES` above is
    built while the module loads, which is before the settings cache is warm,
    so an alphabet captured there would be whatever the fallback happened to
    say. This is one in-memory dict lookup, the same cost `game_for` already
    pays for every configured game.
    """
    return LETTER_GROUPS.get(guild_setting_sync(None, "language"), ())


def first_letter(word: str, letters: tuple[str, ...]) -> str:
    """The word's first letter, which may be more than one character."""
    for candidate in letters:
        if word.startswith(candidate):
            return candidate
    return word[:1]


def last_letter(word: str, letters: tuple[str, ...]) -> str:
    """The word's last letter, which may be more than one character."""
    for candidate in letters:
        if word.endswith(candidate):
            return candidate
    return word[-1:]


def unique_value(game_key: str, value: str) -> str | None:
    """What this turn spends out of the chain's supply, if anything.

    A word may only be played once, and it is folded here for the same reason
    every other comparison folds both sides. Counting spends nothing: the count
    only ever goes up, so its values cannot repeat, and recording them would
    grow a table that can never answer anything.
    """
    return fold(value) if game_key == "word_chain" else None


def fold(word: str) -> str:
    """Compare words without accents or case.

    Hungarian is the primary language here, so case and combining accents both
    have to fall away before two entries can be compared: an accented vowel and
    its bare form are the same letter for the purpose of joining a chain.
    Folding both sides is what stops an argument about whether a turn counted.
    """
    decomposed = unicodedata.normalize("NFD", word.casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


class Minigames(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    def game_for_channel(self, guild_id: int, channel_id: int) -> str | None:
        """Which game this channel is, if any. Cheap enough for every message.

        Shared by `on_message` and `on_raw_message_edit`: an edit has only a
        channel id, never a cached `discord.Message`.
        """
        for game_key, spec in GAMES.items():
            setting_channel_id = guild_setting_sync(guild_id, spec["channel"])
            if setting_channel_id and int(setting_channel_id) == channel_id:
                return game_key
        return None

    def game_for(self, message) -> str | None:
        return self.game_for_channel(message.guild.id, message.channel.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return
        try:
            await self.police(message)
        except Exception:
            # An exception out of a listener stops it for every guild, and this
            # one runs on every message in the installation.
            minigame_logger.exception(
                "Minigame check failed (guild_id=%s, channel_id=%s)",
                message.guild.id, message.channel.id,
            )

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        # Discord omits "content" from the edit payload when only an embed
        # hydrated (a link unfurling) rather than the author retyping
        # anything -- that must not be treated as an edit, or every posted
        # link would be policed off its own embed a moment after acceptance.
        if "content" not in payload.data or payload.guild_id is None:
            return
        try:
            await self.police_edit(payload)
        except Exception:
            minigame_logger.exception(
                "Minigame edit check failed (guild_id=%s, channel_id=%s)",
                payload.guild_id, payload.channel_id,
            )

    async def police_edit(self, payload: discord.RawMessageUpdateEvent):
        game_key = self.game_for_channel(payload.guild_id, payload.channel_id)
        if game_key is None:
            return
        spec = GAMES[game_key]
        if not is_enabled(payload.guild_id, spec["feature"]):
            return
        author_data = payload.data.get("author")
        if not author_data:
            return
        author_id = int(author_data["id"])
        if self.bot.user is not None and author_id == self.bot.user.id:
            return
        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(author_id) if guild else None
        if maintenance_blocks(guild, member):
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None:
            return
        # The DB row a turn wrote is untouched by an edit, so the channel is
        # the only thing that can now disagree with it -- deleted rather than
        # re-judged, since re-validating the new content would still leave a
        # window where the channel showed something the bot never accepted.
        await self._delete_and_notify(
            channel.get_partial_message(payload.message_id), channel,
            author_id, t("minigames.err_edited"))

    async def police(self, message: discord.Message):
        game_key = self.game_for(message)
        if game_key is None:
            return
        spec = GAMES[game_key]
        if not is_enabled(message.guild.id, spec["feature"]):
            return
        # Maintenance is the emergency stop, and a channel the bot is policing
        # is exactly where it must stop policing.
        if maintenance_blocks(message.guild, message.author):
            return

        state = await database.run_read(
            database.get_minigame_state, message.guild.id, game_key)
        verdict = (self.judge_count(message, state) if game_key == "counting"
                   else self.judge_word(message, state))
        if verdict is None:
            return  # chatter, left alone
        accepted, value, reason = verdict

        if not accepted:
            return await self.refuse(message, reason)

        if (state["last_user_id"] == message.author.id
                and not guild_setting_sync(message.guild.id,
                                           "minigame_allow_double_turn")):
            return await self.refuse(message, t("minigames.err_same_person"))

        moved = await database.run_write(
            database.advance_minigame, message.guild.id, game_key, value,
            message.author.id, state["value"], unique_value(game_key, value))
        if moved is None:
            # Somebody else's turn landed first. Theirs stands; this one is a
            # duplicate rather than a mistake, so it is removed with a note that
            # says so.
            return await self.refuse(message, t("minigames.err_raced"))
        if moved.get("duplicate"):
            return await self.refuse(message, t("minigames.err_already_used"))
        # A wrong message is removed rather than breaking the chain, so the
        # streak never resets by itself and `streak == best_streak` always —
        # reacting on a new best would therefore put a trophy on *every*
        # message from the second one onward. A round number is what a counting
        # channel actually celebrates, so that is what is marked.
        if moved["streak"] % MILESTONE_EVERY == 0:
            try:
                await message.add_reaction("🏆")
            except discord.HTTPException:
                pass

    def judge_count(self, message, state):
        match = COUNT_PATTERN.match(message.content)
        if match is None:
            return None
        current = int(state["value"] or 0)
        posted = int(match.group(1))
        if posted != current + 1:
            return False, None, t("minigames.err_next_number",
                                  expected=current + 1)
        return True, str(posted), None

    def judge_word(self, message, state):
        match = WORD_PATTERN.match(message.content)
        if match is None:
            return None
        posted = match.group(1)
        previous = state["value"]
        if previous:
            letters = chain_letters()
            needed = last_letter(fold(previous), letters)
            if first_letter(fold(posted), letters) != needed:
                return False, None, t("minigames.err_next_letter",
                                      letter=needed.upper(), word=previous)
            if fold(posted) == fold(previous):
                # Kept as a fast path even though the used-word claim would
                # catch it too: this one names the word, and a guild upgrading
                # mid-chain has its current word in `minigame_state` and not
                # yet in `minigame_used_words`.
                return False, None, t("minigames.err_same_word")
        language = guild_setting_sync(None, "language")
        folded_posted = fold(posted)
        if wordchain_dictionary.is_known_word(language, folded_posted):
            return True, posted, None
        if self._is_custom_word(message.guild.id, folded_posted):
            return True, posted, None
        return False, None, t("minigames.err_not_a_word")

    def _is_custom_word(self, guild_id: int, folded_word: str) -> bool:
        """A guild-authored top-up on the built-in dictionary, gated by its
        own tickbox so a guild that never configured one behaves exactly as
        before. Folded with the same `fold()` the built-in check already
        applies, so a Hungarian multi-character letter compares the same way
        on both sides."""
        settings = guild_settings_sync(guild_id, (
            "wordchain_custom_words_enabled", "wordchain_custom_words",
        ))
        if not settings.get("wordchain_custom_words_enabled"):
            return False
        return any(fold(word) == folded_word
                   for word in settings.get("wordchain_custom_words") or ())

    async def refuse(self, message: discord.Message, reason: str):
        """Remove the message and tell only its author why.

        The note goes to the author's own view of the channel, so the channel
        reads as the chain and nobody is corrected in public.
        """
        await self._delete_and_notify(message, message.channel,
                                      message.author.id, reason)

    async def _delete_and_notify(self, deletable, channel, author_id: int,
                                 reason: str):
        """Delete a message (or a `PartialMessage` standing in for one an
        edit invalidated) and tell only its author why.

        If the bot cannot delete — no Manage Messages — it says nothing at
        all rather than leaving a correction beside a message that is still
        there, which would read as the bot being broken. The author is
        addressed by id rather than a resolved member, since an edit's raw
        payload does not guarantee one is cached.
        """
        try:
            await deletable.delete()
        except discord.Forbidden:
            minigame_logger.warning(
                "Cannot police a minigame channel without Manage Messages "
                "(guild_id=%s, channel_id=%s)",
                channel.guild.id, channel.id,
            )
            return
        except discord.HTTPException:
            return
        # An ephemeral reply needs an interaction, and a message is not one, so
        # this is a DM-less short-lived channel message addressed to the author
        # and removed again.
        try:
            await channel.send(
                t("minigames.refused", user=f"<@{author_id}>", reason=reason),
                delete_after=8,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, roles=False,
                    users=[discord.Object(id=author_id)]),
            )
        except discord.HTTPException:
            pass

    @commands.hybrid_command(name="minigame_reset",
                             description=t("general.cmd_minigame_reset"))
    @discord.app_commands.default_permissions(manage_guild=True)
    @discord.app_commands.choices(game=MINIGAME_CHOICES)
    async def minigame_reset(self, ctx, game: str):
        """Start a chain over.

        The chain never breaks by itself — a wrong message is removed rather
        than resetting it — so this is the only way back to zero, and an
        operator needs one: a channel configured onto a conversation that was
        already there starts from whatever number happened to be in it.
        """
        if game not in GAMES:
            return await ctx.send(t("minigames.err_unknown_game"),
                                  ephemeral=True)
        await database.run_write(database.reset_minigame, ctx.guild.id, game)
        await ctx.send(t("minigames.reset_done"), ephemeral=True)


async def setup(bot):
    await bot.add_cog(Minigames(bot))
