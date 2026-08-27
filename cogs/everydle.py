import discord
import random
import json
import logging
import os
import sys
import tempfile
import threading
import asyncio

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database
from minigame_data import load_or_disable

from discord.ext import commands
from datetime import datetime
from cogs.utils import (apply_database_result, guild_setting_sync,
                        is_channel, t)
from feature_access import require_interaction_feature
from feature_access import is_enabled

everydle_logger = logging.getLogger("PotatoBot.Everydle")

# Dataset paths are anchored to the repository and do not depend on the launch directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(os.path.dirname(COG_DIR), "data")

# Mechanics use stable IDs; the selected catalog supplies every displayed value and alias.
# Selected at import, which is why changing the language is a subsystem
# reload rather than a live setting.
ACTIVE_LANGUAGE = guild_setting_sync(None, "language")


def load_game_dataset(game, filename, dataset_name):
    game_dir = os.path.join(DATA_DIR, game)
    return load_or_disable(
        os.path.join(game_dir, filename),
        os.path.join(game_dir, "locales", f"{ACTIVE_LANGUAGE}.json"),
        dataset_name,
    )


CHAMPIONS, CHAMPIONS_LOWER = load_game_dataset("loldle", "champions.json", "champions")
LORE_CHAMPS, LORE_CHAMPS_LOWER = load_game_dataset(
    "loldle", "loldlehardmode.json", "hard_mode"
)
AGENTS, AGENTS_LOWER = load_game_dataset("valdle", "valdle.json", "agents")
GENSHIN, GENSHIN_LOWER = load_game_dataset(
    "genshindle", "genshindle.json", "characters")
DBDLE_KILLERS, DBDLE_KILLER_ALIASES = load_game_dataset(
    "dbdle", "killers.json", "killers"
)
DBDLE_DATA = {"killer": DBDLE_KILLERS}
DBDLE_LOWER = {"killer": DBDLE_KILLER_ALIASES}

# Persist shuffled decks so all users receive the same target without immediate repeats.
STATE_FILE = os.path.join(DATA_DIR, "everydle_state.json")
STATE_LOCK = threading.Lock()


def _save_everydle_state(state):
    """Atomically persist daily IDs so interrupted writes cannot erase game state."""
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=DATA_DIR, delete=False
        ) as temp_file:
            json.dump(state, temp_file, indent=4)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = temp_file.name
        os.replace(temp_path, STATE_FILE)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def get_daily_target(game_type: str, full_list: list, legacy_aliases=None) -> str:
    """Return a stable daily entity ID while upgrading legacy name-based state."""
    legacy_aliases = legacy_aliases or {}
    with STATE_LOCK:
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, json.JSONDecodeError):
                state = {"decks": {}, "dailies": {}}
        else:
            state = {"decks": {}, "dailies": {}}

        now_date = datetime.now().strftime("%Y-%m-%d")

        if "dailies" not in state: state["dailies"] = {}
        if "decks" not in state: state["decks"] = {}
        if game_type not in state["dailies"]: state["dailies"][game_type] = {"date": "", "answer": ""}
        if game_type not in state["decks"]: state["decks"][game_type] = []

        state_changed = False
        stored_answer = state["dailies"][game_type]["answer"]
        if stored_answer not in full_list:
            migrated_answer = legacy_aliases.get(str(stored_answer).casefold(), stored_answer)
            state_changed = migrated_answer != stored_answer
            stored_answer = migrated_answer
            state["dailies"][game_type]["answer"] = stored_answer

        original_deck = state["decks"][game_type]
        deck = [
            legacy_aliases.get(str(item).casefold(), item)
            for item in original_deck
        ]
        deck = [item for item in deck if item in full_list]
        if deck != original_deck:
            state["decks"][game_type] = deck
            state_changed = True

        if state["dailies"][game_type]["date"] == now_date and stored_answer in full_list:
            if state_changed:
                _save_everydle_state(state)
            return stored_answer

        if len(deck) == 0:
            deck = list(full_list)
            random.shuffle(deck)

        daily_answer = deck.pop(0)

        state["decks"][game_type] = deck
        state["dailies"][game_type] = {"date": now_date, "answer": daily_answer}

        _save_everydle_state(state)

        return daily_answer

# Hard-mode classes remain dormant; the unfinished mode is not exposed by commands.
class LoldleHardModal(discord.ui.Modal):
    def __init__(self, view_instance):
        super().__init__(title=t("everydle.modal_lore_title"))
        self.guess = discord.ui.TextInput(
            label=t("everydle.modal_lore_label"), 
            placeholder=t("everydle.modal_lore_placeholder"), 
            min_length=1, max_length=20
        )
        self.add_item(self.guess)
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "everydle_loldle"):
            return
        await interaction.response.defer()
        user_input = self.guess.value.strip().casefold()
        if user_input not in LORE_CHAMPS_LOWER:
            return await interaction.followup.send(t("everydle.err_lore_champ_not_found"), ephemeral=True)

        guess_id = LORE_CHAMPS_LOWER[user_input]
        v = self.view_instance
        target_id = v.target_champ
        guess_data = LORE_CHAMPS[guess_id]
        target_data = LORE_CHAMPS[target_id]

        g_year, t_year = guess_data["year"], target_data["year"]
        if g_year == t_year: year_str = f"{g_year}\u00A0🟩"
        elif g_year < t_year: year_str = f"{g_year}\u00A0🔼"
        else: year_str = f"{g_year}\u00A0🔽"

        v.guess_history.append(f"**{guess_data['_name']}** | {year_str}")
        header = t("everydle.lore_header")

        if guess_id == target_id:
            # Claim the reward transactionally before announcing a successful answer.
            base_coin, base_xp = await database.run(
                database.get_reward, interaction.guild_id, "loldle_hard", 7500, 150
            )
            if not is_enabled(interaction.guild_id, "levels"):
                base_xp = 0
            now_iso = datetime.now().isoformat()
            result = await database.run(database.claim_everydle_reward,
                interaction.user.id, "last_loldle_hard", now_iso, base_coin, base_xp,
                interaction.guild_id
            )
            if not result["claimed"]:
                for child in v.children: child.disabled = True
                return await interaction.edit_original_response(
                    content=t("everydle.err_loldle_played", diff_text=t("everydle.diff_hard")),
                    view=v,
                )
            reward = result["reward"]
            await apply_database_result(interaction.user, result)

            for child in v.children: child.disabled = True
            win_content = t("everydle.lore_win_msg", quote=target_data['quote'], header=header, history="\n".join(v.guess_history), reward=reward)
            await interaction.edit_original_response(content=win_content, view=v)
            await interaction.channel.send(t("everydle.lore_public_win", user=interaction.user.display_name, count=len(v.guess_history), reward=reward))
        else:
            board_content = t("everydle.lore_board", quote=target_data['quote'], header=header, history="\n".join(v.guess_history))
            await interaction.edit_original_response(content=board_content, view=v)

class LoldleHardView(discord.ui.View):
    def __init__(self, target_champ):
        super().__init__(timeout=15 * 60)
        self.target_champ = target_champ
        self.guess_history = []
        
        btn = discord.ui.Button(label=t("everydle.btn_guess"), style=discord.ButtonStyle.primary, emoji="📖")
        btn.callback = self.guess_btn
        self.add_item(btn)

    async def guess_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LoldleHardModal(self))

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "everydle_loldle")

# Standard LoLdle interaction state.
class LoldleView(discord.ui.View):
    def __init__(self, target_champ, difficulty):
        super().__init__(timeout=15 * 60)
        self.target_champ = target_champ
        self.difficulty = difficulty 
        self.guess_history = [] 

        btn = discord.ui.Button(label=t("everydle.btn_guess"), style=discord.ButtonStyle.primary, emoji="🤔")
        btn.callback = self.guess_btn
        self.add_item(btn)

    async def guess_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(LoldleModal(self))

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "everydle_loldle")

# Standard LoLdle guess submission.
class LoldleModal(discord.ui.Modal):
    def __init__(self, view_instance):
        super().__init__(title=t("everydle.modal_loldle_title"))
        self.guess = discord.ui.TextInput(
            label=t("everydle.modal_loldle_label"),
            placeholder=t("everydle.modal_loldle_placeholder"),
            min_length=1, max_length=20
        )
        self.add_item(self.guess)
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "everydle_loldle"):
            return
        await interaction.response.defer()
        user_input = self.guess.value.strip().casefold()
        if user_input not in CHAMPIONS_LOWER:
            return await interaction.followup.send(t("everydle.err_champ_not_found"), ephemeral=True)

        guess_id = CHAMPIONS_LOWER[user_input]
        v = self.view_instance
        target_id = v.target_champ
        target_data = CHAMPIONS[target_id]
        guess_data = CHAMPIONS[guess_id]

        is_easy = (v.difficulty == "easy")
        diff_name = t("everydle.diff_easy") if is_easy else t("everydle.diff_medium")
        diff_icon = "🟢" if is_easy else "🟡"
        
        # Claim the reward transactionally before announcing a successful answer.
        if is_easy:
            base_coin, base_xp = await database.run(
                database.get_reward, interaction.guild_id, "loldle_easy", 2500, 100
            )
        else:
            base_coin, base_xp = await database.run(
                database.get_reward, interaction.guild_id, "loldle_medium", 5000, 100
            )
            
        def check_match(guess_val, target_val):
            if not isinstance(guess_val, list): guess_val = [guess_val]
            if not isinstance(target_val, list): target_val = [target_val]
            set_g = set(guess_val)
            set_t = set(target_val)
            display_text = ", ".join(str(x) for x in guess_val)
            if set_g == set_t: return f"{display_text}\u00A0🟩"
            elif set_g.intersection(set_t): return f"{display_text}\u00A0🟨"
            else: return f"{display_text}\u00A0🟥"

        row_parts = [
            f"**{guess_data['_name']}**",
            check_match(guess_data.get("gender", []), target_data.get("gender", [])),
            check_match(guess_data.get("role", []), target_data.get("role", [])),
            check_match(guess_data.get("species", []), target_data.get("species", [])),
            check_match(guess_data.get("resource", []), target_data.get("resource", [])),
            check_match(guess_data.get("range", []), target_data.get("range", [])),
            check_match(guess_data.get("region", []), target_data.get("region", []))
        ]
        
        g_year, t_year = guess_data["year"], target_data["year"]
        if g_year == t_year: row_parts.append(f"{g_year}\u00A0🟩")
        elif g_year < t_year: row_parts.append(f"{g_year}\u00A0🔼")
        else: row_parts.append(f"{g_year}\u00A0🔽")

        v.guess_history.append(" | ".join(row_parts))

        header_text = t("everydle.loldle_header")
        history_joined = "\n".join(v.guess_history)

        def format_target_stats():
            def j(val): return ", ".join(str(x) for x in val) if isinstance(val, list) else val
            return f"**???** | {j(target_data.get('gender', []))} | {j(target_data.get('role', []))} | {j(target_data.get('species', []))} | {j(target_data.get('resource', []))} | {j(target_data.get('range', []))} | {j(target_data.get('region', []))} | {target_data.get('year', '?')}"

        if guess_id == target_id:
            col_name = f"last_loldle_{v.difficulty}"
            if not is_enabled(interaction.guild_id, "levels"):
                base_xp = 0
            now_iso = datetime.now().isoformat()
            result = await database.run(database.claim_everydle_reward,
                interaction.user.id, col_name, now_iso, base_coin, base_xp,
                interaction.guild_id
            )
            if not result["claimed"]:
                for child in v.children: child.disabled = True
                return await interaction.edit_original_response(
                    content=t("everydle.err_loldle_played", diff_text=diff_name),
                    embed=None, view=v,
                )
            reward = result["reward"]
            await apply_database_result(interaction.user, result)

            win_content = t("everydle.loldle_win_msg", diff_name=diff_name, header=header_text, history=history_joined, reward=reward)
            
            for child in v.children:
                child.disabled = True

            await interaction.edit_original_response(content=win_content, embed=None, view=v)
            await interaction.channel.send(t("everydle.loldle_public_win", user=interaction.user.display_name, diff_name=diff_name, count=len(v.guess_history), reward=reward))
        else:
            target_stats_str = f"{format_target_stats()}\n\n" if is_easy else ""
            board_content = t("everydle.loldle_board", icon=diff_icon, diff_name=diff_name, header=header_text, target_stats=target_stats_str, history=history_joined)
            await interaction.edit_original_response(content=board_content, embed=None, view=v)

# Valdle guess submission and comparison rendering.
class ValdleModal(discord.ui.Modal):
    def __init__(self, view_instance):
        super().__init__(title=t("everydle.modal_valdle_title"))
        self.guess = discord.ui.TextInput(
            label=t("everydle.modal_valdle_label"), 
            placeholder=t("everydle.modal_valdle_placeholder"), 
            min_length=2
        )
        self.add_item(self.guess)
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "everydle_valdle"):
            return
        await interaction.response.defer()
        user_input = self.guess.value.strip().casefold()
        if user_input not in AGENTS_LOWER:
            return await interaction.followup.send(t("everydle.err_agent_not_found"), ephemeral=True)

        guess_id = AGENTS_LOWER[user_input]
        v = self.view_instance
        target_data = AGENTS[v.target_agent]
        guess_data = AGENTS[guess_id]

        def check_match(guess_val, target_val):
            if guess_val == target_val: return f"{guess_val}\u00A0🟩"
            return f"{guess_val}\u00A0🟥"

        row = [
            f"**{guess_data['_name']}**",
            check_match(guess_data["gender"], target_data["gender"]),
            check_match(guess_data["role"], target_data["role"]),
            check_match(guess_data["species"], target_data["species"]),
            check_match(guess_data["origin"], target_data["origin"])
        ]

        g_year, t_year = guess_data["year"], target_data["year"]
        if g_year == t_year: row.append(f"{g_year}\u00A0🟩")
        else: row.append(f"{g_year}\u00A0{'🔼' if g_year < t_year else '🔽'}")

        v.guess_history.append(" | ".join(row))
        header = t("everydle.valdle_header")

        if guess_id == v.target_agent:
            # Claim the reward transactionally before announcing a successful answer.
            base_coin, base_xp = await database.run(
                database.get_reward, interaction.guild_id, "valdle", 5000, 100
            )
            if not is_enabled(interaction.guild_id, "levels"):
                base_xp = 0
            now_iso = datetime.now().isoformat()
            result = await database.run(database.claim_everydle_reward,
                interaction.user.id, "last_valdle", now_iso, base_coin, base_xp,
                interaction.guild_id
            )
            if not result["claimed"]:
                for child in v.children: child.disabled = True
                return await interaction.edit_original_response(
                    content=t("everydle.err_valdle_played"), view=v
                )
            reward = result["reward"]
            await apply_database_result(interaction.user, result)

            for child in v.children: 
                child.disabled = True
                
            win_content = t("everydle.valdle_win_msg", header=header, history="\n".join(v.guess_history), reward=reward)
            await interaction.edit_original_response(content=win_content, view=v)
            await interaction.channel.send(t("everydle.valdle_public_win", user=interaction.user.display_name, count=len(v.guess_history), reward=reward))
        else:
            board_content = t("everydle.valdle_board", header=header, history="\n".join(v.guess_history))
            await interaction.edit_original_response(content=board_content, view=v)

# Genshindle: one Genshin character a day, compared attribute by attribute.
#
# Every attribute is published by the game, so unlike Valdle and DbDle this
# dataset carries no lore and `scripts/everydle_sources.py` can keep it current
# on its own. `version` is the release version stored as major*100 + minor, so
# it compares higher/lower the way Valdle's year does — "1.0" and "5.3" sort
# wrong as text, and 1.10 would collide with 1.1 as a float.
# `body_type` was dropped (2026-08-27): five values across 120 characters, most
# of them in two, so it took a column and narrowed almost nothing. Removed from
# the dataset and from the updater's field list too, so it cannot come back as a
# drift finding.
GENSHIN_FIELDS = ("element", "weapon", "region", "rarity", "gender",
                  "weekly_boss")


def genshin_version_text(packed: int) -> str:
    """`402` back to `4.2`, for a row a player reads."""
    return f"{packed // 100}.{packed % 100}"


class GenshindleModal(discord.ui.Modal):
    def __init__(self, view_instance):
        super().__init__(title=t("everydle.modal_genshindle_title"))
        self.guess = discord.ui.TextInput(
            label=t("everydle.modal_genshindle_label"),
            placeholder=t("everydle.modal_genshindle_placeholder"),
            min_length=2,
        )
        self.add_item(self.guess)
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "everydle_genshindle"):
            return
        await interaction.response.defer()
        user_input = self.guess.value.strip().casefold()
        if user_input not in GENSHIN_LOWER:
            return await interaction.followup.send(
                t("everydle.err_character_not_found"), ephemeral=True)

        guess_id = GENSHIN_LOWER[user_input]
        v = self.view_instance
        target_data = GENSHIN[v.target_character]
        guess_data = GENSHIN[guess_id]

        def check_match(guess_val, target_val):
            if guess_val == target_val:
                return f"{guess_val}\u00A0🟩"
            return f"{guess_val}\u00A0🟥"

        row = [f"**{guess_data['_name']}**"]
        row.extend(check_match(guess_data[field], target_data[field])
                   for field in GENSHIN_FIELDS)

        g_version, t_version = guess_data["version"], target_data["version"]
        shown = genshin_version_text(g_version)
        if g_version == t_version:
            row.append(f"{shown}\u00A0🟩")
        else:
            row.append(f"{shown}\u00A0{'🔼' if g_version < t_version else '🔽'}")

        v.guess_history.append(" | ".join(row))
        header = t("everydle.genshindle_header")

        if guess_id == v.target_character:
            # Claim the reward transactionally before announcing a win.
            base_coin, base_xp = await database.run(
                database.get_reward, interaction.guild_id, "genshindle", 5000, 100
            )
            if not is_enabled(interaction.guild_id, "levels"):
                base_xp = 0
            now_iso = datetime.now().isoformat()
            result = await database.run(
                database.claim_everydle_reward, interaction.user.id,
                "last_genshindle", now_iso, base_coin, base_xp,
                interaction.guild_id,
            )
            if not result["claimed"]:
                for child in v.children:
                    child.disabled = True
                return await interaction.edit_original_response(
                    content=t("everydle.err_genshindle_played"), view=v)
            reward = result["reward"]
            await apply_database_result(interaction.user, result)

            for child in v.children:
                child.disabled = True
            win_content = t("everydle.genshindle_win_msg", header=header,
                            history="\n".join(v.guess_history), reward=reward)
            await interaction.edit_original_response(content=win_content, view=v)
            await interaction.channel.send(t(
                "everydle.genshindle_public_win",
                user=interaction.user.display_name,
                count=len(v.guess_history), reward=reward))
        else:
            board_content = t("everydle.genshindle_board", header=header,
                              history="\n".join(v.guess_history))
            await interaction.edit_original_response(content=board_content, view=v)


class GenshindleView(discord.ui.View):
    def __init__(self, target_character):
        super().__init__(timeout=15 * 60)
        self.target_character = target_character
        self.guess_history = []

        btn = discord.ui.Button(label=t("everydle.btn_guess"),
                                style=discord.ButtonStyle.primary, emoji="🎯")
        btn.callback = self.guess_btn
        self.add_item(btn)

    async def guess_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(GenshindleModal(self))

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "everydle_genshindle")


class ValdleView(discord.ui.View):
    def __init__(self, target_agent):
        super().__init__(timeout=15 * 60)
        self.target_agent = target_agent
        self.guess_history = []

        btn = discord.ui.Button(label=t("everydle.btn_guess"), style=discord.ButtonStyle.primary, emoji="🎯")
        btn.callback = self.guess_btn
        self.add_item(btn)

    async def guess_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ValdleModal(self))

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "everydle_valdle")

# DbDle killer guess submission and interaction state.
class DbdleModal(discord.ui.Modal):
    def __init__(self, view_instance):
        super().__init__(title=t("everydle.modal_dbdle_title"))
        self.guess = discord.ui.TextInput(
            label=t("everydle.modal_dbdle_label"), 
            placeholder=t("everydle.modal_dbdle_placeholder"), 
            min_length=2
        )
        self.add_item(self.guess)
        self.view_instance = view_instance

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "everydle_dbdle"):
            return
        await interaction.response.defer()
        user_input = self.guess.value.strip().casefold()
        
        if user_input not in DBDLE_LOWER["killer"]:
            return await interaction.followup.send(t("everydle.err_killer_not_found"), ephemeral=True)

        guess_id = DBDLE_LOWER["killer"][user_input]
        v = self.view_instance
        target_data = DBDLE_DATA["killer"][v.target_item]
        guess_data = DBDLE_DATA["killer"][guess_id]

        def check_match(guess_val, target_val):
            if guess_val == target_val: return f"{guess_val}\u00A0🟩"
            return f"{guess_val}\u00A0🟥"

        row = [
            f"**{guess_data['_name']}**",
            check_match(guess_data.get("gender"), target_data.get("gender")),
            check_match(guess_data.get("movement_speed"), target_data.get("movement_speed")),
            check_match(guess_data.get("terror_radius"), target_data.get("terror_radius")),
            check_match(guess_data.get("height"), target_data.get("height")),
            check_match(guess_data.get("country"), target_data.get("country"))
        ]

        try:
            g_year = int(guess_data.get("release", 0))
            t_year = int(target_data.get("release", 0))
            if g_year == t_year: 
                row.append(f"{g_year}\u00A0🟩")
            elif g_year < t_year: 
                row.append(f"{g_year}\u00A0🔼")
            else: 
                row.append(f"{g_year}\u00A0🔽")
        except ValueError:
            row.append(check_match(guess_data.get("release"), target_data.get("release")))

        header = t("everydle.dbdle_header")
        v.guess_history.append(" | ".join(row))

        if guess_id == v.target_item:
            # Claim the reward transactionally before announcing a successful answer.
            base_coin, base_xp = await database.run(
                database.get_reward, interaction.guild_id, "dbdle", 5000, 100
            )
            if not is_enabled(interaction.guild_id, "levels"):
                base_xp = 0
            now_iso = datetime.now().isoformat()
            result = await database.run(database.claim_everydle_reward,
                interaction.user.id, "last_dbdle_killer", now_iso, base_coin, base_xp,
                interaction.guild_id
            )
            if not result["claimed"]:
                for child in v.children: child.disabled = True
                return await interaction.edit_original_response(
                    content=t("everydle.err_dbdle_played"), view=v
                )
            reward = result["reward"]
            await apply_database_result(interaction.user, result)

            for child in v.children: child.disabled = True
                
            win_content = t("everydle.dbdle_win_msg", header=header, history="\n".join(v.guess_history), reward=reward)
            await interaction.edit_original_response(content=win_content, view=v)
            await interaction.channel.send(t("everydle.dbdle_public_win", user=interaction.user.display_name, count=len(v.guess_history), reward=reward))
        else:
            board_content = t("everydle.dbdle_board", header=header, history="\n".join(v.guess_history))
            await interaction.edit_original_response(content=board_content, view=v)

class DbdleView(discord.ui.View):
    def __init__(self, target_item):
        super().__init__(timeout=15 * 60)
        self.target_item = target_item
        self.guess_history = []

        btn = discord.ui.Button(label=t("everydle.btn_guess"), style=discord.ButtonStyle.danger, emoji="🔪")
        btn.callback = self.guess_btn
        self.add_item(btn)

    async def guess_btn(self, interaction: discord.Interaction):
        await interaction.response.send_modal(DbdleModal(self))

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "everydle_dbdle")

class Everydle(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="loldle", description=t("general.cmd_loldle"))
    @discord.app_commands.choices(difficulty=[
        discord.app_commands.Choice(name=t("everydle.diff_choice_easy"), value="easy"),
        discord.app_commands.Choice(name=t("everydle.diff_choice_medium"), value="medium")
    ])
    @is_channel("everydle_channel")
    async def loldle(self, ctx, difficulty: str = "medium"): 
        if not CHAMPIONS:
            return await ctx.send(t("everydle.err_champions_json"), ephemeral=True)
        
        user_id = ctx.author.id
        now = datetime.now()
    
        if difficulty not in ["easy", "medium"]:
            return await ctx.send(t("everydle.err_invalid_diff"), ephemeral=True)

        col_name = f"last_loldle_{difficulty}"
        
        last_play_str = await database.run(database.get_cooldown, user_id, col_name)
        if last_play_str:
            last_play = datetime.fromisoformat(last_play_str)
            if last_play.date() == now.date():
                diff_text = t("everydle.diff_easy") if difficulty == "easy" else t("everydle.diff_medium")
                return await ctx.send(t("everydle.err_loldle_played", diff_text=diff_text), ephemeral=True)

        daily_champ = await asyncio.to_thread(
            get_daily_target, f"loldle_{difficulty}", list(CHAMPIONS.keys()),
            CHAMPIONS_LOWER,
        )
        view = LoldleView(target_champ=daily_champ, difficulty=difficulty)
        header_text = t("everydle.loldle_header")
        
        if difficulty == "easy":
            t_data = CHAMPIONS[daily_champ]
            def j(val): return ", ".join(str(x) for x in val) if isinstance(val, list) else val
            target_row = f"**???** | {j(t_data.get('gender', []))} | {j(t_data.get('role', []))} | {j(t_data.get('species', []))} | {j(t_data.get('resource', []))} | {j(t_data.get('range', []))} | {j(t_data.get('region', []))} | {t_data.get('year', '?')}"
            initial_content = t("everydle.loldle_easy_init", header=header_text, target_row=target_row)
        else:
            initial_content = t("everydle.loldle_med_init", header=header_text)
    
        await ctx.send(content=initial_content, view=view, ephemeral=True)

    @commands.hybrid_command(name="valdle", description=t("general.cmd_valdle"))
    @is_channel("everydle_channel")
    async def valdle(self, ctx):
        if not AGENTS:
            return await ctx.send(t("everydle.err_valdle_json"), ephemeral=True)

        user_id = ctx.author.id
        now = datetime.now()

        last_play_str = await database.run(database.get_cooldown, user_id, "last_valdle")
        if last_play_str:
            last_play = datetime.fromisoformat(last_play_str)
            if last_play.date() == now.date():
                return await ctx.send(t("everydle.err_valdle_played"), ephemeral=True)

        daily_agent = await asyncio.to_thread(
            get_daily_target, "valdle", list(AGENTS.keys()), AGENTS_LOWER
        )
        view = ValdleView(target_agent=daily_agent)
        
        header = t("everydle.valdle_header")
        await ctx.send(content=t("everydle.valdle_init", header=header), view=view, ephemeral=True)

    @commands.hybrid_command(name="genshindle",
                             description=t("general.cmd_genshindle"))
    @is_channel("everydle_channel")
    async def genshindle(self, ctx):
        if not GENSHIN:
            return await ctx.send(t("everydle.err_genshindle_json"), ephemeral=True)

        user_id = ctx.author.id
        now = datetime.now()

        last_play_str = await database.run(
            database.get_cooldown, user_id, "last_genshindle")
        if last_play_str:
            last_play = datetime.fromisoformat(last_play_str)
            if last_play.date() == now.date():
                return await ctx.send(t("everydle.err_genshindle_played"),
                                      ephemeral=True)

        daily_character = await asyncio.to_thread(
            get_daily_target, "genshindle", list(GENSHIN.keys()), GENSHIN_LOWER
        )
        view = GenshindleView(target_character=daily_character)
        header = t("everydle.genshindle_header")
        await ctx.send(content=t("everydle.genshindle_init", header=header),
                       view=view, ephemeral=True)

    @commands.hybrid_command(name="dbdle", description=t("general.cmd_dbdle"))
    @is_channel("everydle_channel")
    async def dbdle(self, ctx):
        user_id = ctx.author.id
        now = datetime.now()

        if not DBDLE_DATA["killer"]:
            return await ctx.send(t("everydle.err_dbdle_json"), ephemeral=True)

        last_play_str = await database.run(database.get_cooldown, user_id, "last_dbdle_killer")
        if last_play_str:
            last_play = datetime.fromisoformat(last_play_str)
            if last_play.date() == now.date():
                return await ctx.send(t("everydle.err_dbdle_played"), ephemeral=True)

        daily_item = await asyncio.to_thread(
            get_daily_target, "dbdle_killer",
            list(DBDLE_DATA["killer"].keys()),
            DBDLE_LOWER["killer"],
        )
        view = DbdleView(target_item=daily_item)
        
        header = t("everydle.dbdle_header")
        await ctx.send(content=t("everydle.dbdle_init", header=header), view=view, ephemeral=True)

async def setup(bot):
    await bot.add_cog(Everydle(bot))
