import discord
import asyncio
import secrets
import os
import time
import sys

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database

from discord.ext import commands
from datetime import datetime, timedelta
from cogs.utils import (
    BoundedCooldownMap, apply_database_result, currency_emoji, is_channel,
    is_premium, t, config,
)
from feature_access import require_interaction_feature

slots_cooldowns = BoundedCooldownMap()
RNG = secrets.SystemRandom()

CASINO_LAUNCHER_FEATURES = {
    "start_bj_game": "casino_blackjack",
    "start_dice_game": "casino_dice",
    "start_roulette_game": "casino_roulette",
    "start_slots_game": "casino_slots",
    "start_mines_game": "casino_mines",
    "start_free_mines": "casino_mines",
}

def work_settings(stored: dict) -> dict[str, int]:
    """This guild's `/work` numbers, defaulting from the registry per key."""
    from settings_registry import SETTING_DEFINITIONS

    resolved = {}
    for key, definition in SETTING_DEFINITIONS.items():
        if not key.startswith("work_"):
            continue
        row = stored.get(key)
        value = row["value"] if row else definition.default
        resolved[key] = (
            value if isinstance(value, int) and not isinstance(value, bool)
            else definition.default
        )
    return resolved


def weighted_tier(weights: dict[str, int]) -> str:
    """Draw one outcome. All-zero weights fall back to the ordinary shift."""
    total = sum(max(0, weight) for weight in weights.values())
    if total <= 0:
        return "normal"
    point = RNG.randrange(total)
    for tier, weight in weights.items():
        point -= max(0, weight)
        if point < 0:
            return tier
    return "normal"


def random_payout(low: int, high: int) -> int:
    """One payout inside a configured range, tolerating an inverted range."""
    low, high = min(low, high), max(low, high)
    return RNG.randint(low, high) if high > low else high


def work_tier_weights(settings: dict) -> dict[str, int]:
    """Rarity of each `/work` outcome, from this guild's typed settings.

    The three weights are drawn against each other, so the shipped
    998/1/1 reproduces the previous hard-coded one-in-a-thousand chances.
    """
    return {
        tier: int(settings.get(f"work_tier_{tier}_weight", default))
        for tier, default in (("normal", 998), ("free", 1), ("high", 1))
    }


def work_response_text(tier: str, stored: list[dict], earnings: int) -> str:
    """Pick one response for a tier and substitute the earnings into it.

    Every response lives in `work_responses`, so there is one place to look and
    one place to edit. A guild's own rows replace the installation defaults for
    that tier only, which is why the choice is made per tier rather than per
    guild: writing your own "big payday" lines does not blank the other two.

    A response has no language dimension by design — a guild's flavour text is
    that guild's, and a guild speaks one language — so the text is taken as
    written. It is operator-supplied and reaches message content, so it is
    escaped, and the placeholder is substituted with a literal replace rather
    than `str.format` so a stray brace cannot raise here.
    """
    for scope in ("guild", "default"):
        candidates = [
            row for row in stored
            if row["tier"] == tier and row.get("scope", "guild") == scope
            and row["enabled"] and row["weight"] > 0
        ]
        if not candidates:
            continue
        point = RNG.randrange(sum(row["weight"] for row in candidates))
        for row in candidates:
            point -= row["weight"]
            if point < 0:
                message = discord.utils.escape_mentions(row["message"])
                return message.replace(
                    database.WORK_EARNINGS_PLACEHOLDER, str(earnings)
                ).replace(
                    database.WORK_COIN_PLACEHOLDER, currency_emoji()
                )
    # An installation whose defaults were all deleted has nothing to say, and a
    # blank embed description is rejected by Discord.
    return t("casino.work_no_response")


async def require_casino_feature(interaction, launcher):
    feature_key = CASINO_LAUNCHER_FEATURES.get(getattr(launcher, "__name__", ""))
    # An unmapped launcher still has to clear maintenance, so never short-circuit.
    return await require_interaction_feature(interaction, feature_key)

# Shared bet-entry modal used by replay controls across casino games.
class UniversalNewBetModal(discord.ui.Modal):
    def __init__(self, user, game_launcher, *args):
        super().__init__(title=t("casino.new_bet_modal_title"))
        self.new_bet = discord.ui.TextInput(
            label=t("casino.new_bet_label"),
            placeholder=t("casino.new_bet_placeholder"),
            min_length=1, max_length=10
        )
        self.add_item(self.new_bet)
        self.user = user
        self.game_launcher = game_launcher 
        self.args = args

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_casino_feature(interaction, self.game_launcher):
            return
        try:
            val = self.new_bet.value.lower()
            bet_amount = val if val == "all" else int(val)
            
            if isinstance(bet_amount, int) and bet_amount <= 0:
                return await interaction.response.send_message(t("casino.err_bet_min"), ephemeral=True)
            
            await self.game_launcher(interaction, bet_amount, *self.args)
        except ValueError:
            await interaction.response.send_message(t("casino.err_invalid_number_all"), ephemeral=True)

# Shared replay view; each game callback still performs its own atomic wager reservation.
class PlayAgainView(discord.ui.View):
    def __init__(self, user, last_bet, game_launcher, *args):
        super().__init__(timeout=120.0)
        self.user = user
        self.last_bet = last_bet
        self.game_launcher = game_launcher
        self.args = args
        
        btn_again = discord.ui.Button(label=t("casino.btn_play_again"), style=discord.ButtonStyle.primary, emoji="🔄")
        btn_again.callback = self.btn_play_again
        self.add_item(btn_again)
        
        btn_new = discord.ui.Button(label=t("casino.btn_new_bet"), style=discord.ButtonStyle.secondary, emoji="🪙")
        btn_new.callback = self.btn_new_bet
        self.add_item(btn_new)

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_casino_feature(interaction, self.game_launcher)

    async def btn_play_again(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        await self.game_launcher(interaction, self.last_bet, *self.args)

    async def btn_new_bet(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        await interaction.response.send_modal(UniversalNewBetModal(self.user, self.game_launcher, *self.args))

def create_deck():
    suits = ["♠️", "♥️", "♦️", "♣️"]
    ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    deck = []
    for suit in suits:
        for rank in ranks:
            value = 11 if rank == "A" else 10 if rank in ["J", "Q", "K"] else int(rank)
            deck.append({"name": f"{rank}{suit}", "value": value})
    RNG.shuffle(deck)
    return deck

def calc_score(hand):
    score = sum(card["value"] for card in hand)
    aces = sum(1 for card in hand if card["value"] == 11)
    while score > 21 and aces > 0:
        score -= 10
        aces -= 1
    return score

def format_hand(hand, hidden=False):
    if hidden:
        return f"{hand[0]['name']} | 🎴 ?"
    return " | ".join([card["name"] for card in hand])

async def start_bj_game(ctx_or_int, bet):
    user = ctx_or_int.user if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.author
    guild = ctx_or_int.guild
    bet = int(bet)
    wager_id = secrets.token_urlsafe(24)
    remaining_balance = await database.run(
        database.begin_interactive_wager,
        wager_id, guild.id, user.id, "blackjack", bet,
    )
    if remaining_balance is None:
        bal = await database.run(database.get_user_balance, user.id)
        msg = t("casino.err_poor", bal=bal)
        if isinstance(ctx_or_int, discord.Interaction):
            return await ctx_or_int.response.send_message(msg, ephemeral=True)
        else:
            return await ctx_or_int.send(msg, ephemeral=True)

    view = BlackjackView(user, bet, remaining_balance, wager_id)
    
    p_score = calc_score(view.player_hand)
    if p_score == 21:
        view.settled = True
        view.stop()
        winnings = int(bet * 1.5) 
        result = await database.run(
            database.resolve_interactive_wager,
            wager_id, user.id, credit=bet + winnings, win_inc=1,
            outcome="natural_blackjack",
        )
        new_bal, _, _, _, _ = await apply_database_result(user, result)
        
        embed = view.build_embed(game_over=True, result_msg=t("casino.bj_natural_win", winnings=winnings, new_bal=new_bal), color=discord.Color.gold())
        
        play_again_view = PlayAgainView(user, bet, start_bj_game)
        if isinstance(ctx_or_int, discord.Interaction):
            return await ctx_or_int.response.edit_message(embed=embed, view=play_again_view)
        else:
            return await ctx_or_int.send(embed=embed, view=play_again_view)

    if remaining_balance < bet:
        view.double_down.disabled = True
        
    embed = view.build_embed()
    
    try:
        if isinstance(ctx_or_int, discord.Interaction):
            await ctx_or_int.response.edit_message(embed=embed, view=view)
            view.message = await ctx_or_int.original_response()
        else:
            view.message = await ctx_or_int.send(embed=embed, view=view)
    except Exception:
        await database.run(
            database.refund_interactive_wager, wager_id, "delivery_failed"
        )
        raise

class BlackjackView(discord.ui.View):
    def __init__(self, user, bet, balance, wager_id):
        super().__init__(timeout=60.0) 
        self.user = user 
        self.bet = bet
        self.balance = balance
        self.wager_id = wager_id
        self.deck = create_deck() 
        self.settled = False
        self.action_lock = asyncio.Lock()
        self.settlement_lock = asyncio.Lock()
        
        self.player_hand = [self.deck.pop(), self.deck.pop()]
        self.dealer_hand = [self.deck.pop(), self.deck.pop()]

        self.btn_hit = discord.ui.Button(label=t("casino.bj_btn_hit"), style=discord.ButtonStyle.primary, custom_id="bj_hit")
        self.btn_hit.callback = self.hit; self.add_item(self.btn_hit)

        self.btn_stand = discord.ui.Button(label=t("casino.bj_btn_stand"), style=discord.ButtonStyle.secondary, custom_id="bj_stand")
        self.btn_stand.callback = self.stand; self.add_item(self.btn_stand)

        self.double_down = discord.ui.Button(label=t("casino.bj_btn_double"), style=discord.ButtonStyle.success, custom_id="bj_double")
        self.double_down.callback = self.double_down_cb; self.add_item(self.double_down)

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "casino_blackjack")

    def build_embed(self, game_over=False, result_msg="", color=discord.Color.blue()):
        embed = discord.Embed(title=t("casino.bj_title"), description=result_msg, color=color)
        p_score = calc_score(self.player_hand)
        embed.add_field(name=t("casino.bj_player_hand", score=p_score), value=format_hand(self.player_hand), inline=False)

        if game_over:
            d_score = calc_score(self.dealer_hand)
            embed.add_field(name=t("casino.bj_dealer_hand", score=d_score), value=format_hand(self.dealer_hand), inline=False)
        else:
            d_visible_score = self.dealer_hand[0]["value"]
            if d_visible_score == 11:
                d_visible_score = t("casino.bj_ace_score")
            embed.add_field(name=t("casino.bj_dealer_hand_hidden", score=d_visible_score), value=format_hand(self.dealer_hand, hidden=True), inline=False)
            
        embed.set_footer(text=t("casino.bj_footer", bet=self.bet))
        return embed

    async def disable_all_and_end(self, interaction, result_msg, color, win_status):
        async with self.settlement_lock:
            if self.settled:
                return
            self.settled = True
            self.stop()
            credit, win_inc, loss_inc = 0, 0, 0
            if win_status == "win":
                credit, win_inc = self.bet * 2, 1
            elif win_status == "draw":
                credit = self.bet
            else:
                loss_inc = 1
            try:
                result = await database.run(
                    database.resolve_interactive_wager,
                    self.wager_id, self.user.id, credit=credit,
                    win_inc=win_inc, loss_inc=loss_inc, outcome=win_status,
                )
            except Exception:
                self.settled = False
                raise
        if result is None:
            # The wager left "pending" elsewhere, typically a startup refund
            # after a lost view. Report it instead of unpacking a missing row.
            return await interaction.response.send_message(
                t("casino.err_round_already_settled"), ephemeral=True
            )
        new_bal, _, _, _, _ = await apply_database_result(self.user, result)
        result_msg += t("casino.bj_new_bal_suffix", new_bal=new_bal)
        embed = self.build_embed(game_over=True, result_msg=result_msg, color=color)
        
        play_again_view = PlayAgainView(self.user, self.bet, start_bj_game)
        await interaction.response.edit_message(embed=embed, view=play_again_view)

    async def hit(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        async with self.action_lock:
            if self.settled:
                return
            self.double_down.disabled = True
            self.player_hand.append(self.deck.pop())
            p_score = calc_score(self.player_hand)

            if p_score > 21:
                await self.disable_all_and_end(interaction, t("casino.bj_bust", bet=self.bet), discord.Color.red(), "loss")
            else:
                await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def stand(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        async with self.action_lock:
            if self.settled:
                return
            await self.process_dealer(interaction)

    async def double_down_cb(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        async with self.action_lock:
            if self.settled:
                return
            extra_stake = self.bet
            remaining_balance = await database.run(
                database.increase_interactive_wager,
                self.wager_id, self.user.id, extra_stake,
            )
            if remaining_balance is None:
                return await interaction.response.send_message(t("casino.bj_err_no_money_double"), ephemeral=True)

            self.bet += extra_stake
            self.balance = remaining_balance
            self.player_hand.append(self.deck.pop())
            p_score = calc_score(self.player_hand)

            if p_score > 21:
                await self.disable_all_and_end(interaction, t("casino.bj_double_bust", bet=self.bet), discord.Color.red(), "loss")
            else:
                await self.process_dealer(interaction)

    async def process_dealer(self, interaction):
        p_score = calc_score(self.player_hand)
        while calc_score(self.dealer_hand) < 17:
            self.dealer_hand.append(self.deck.pop())
            
        d_score = calc_score(self.dealer_hand)

        if d_score > 21:
            await self.disable_all_and_end(interaction, t("casino.bj_dealer_bust", bet=self.bet), discord.Color.green(), "win")
        elif p_score > d_score:
            await self.disable_all_and_end(interaction, t("casino.bj_player_win", p_score=p_score, d_score=d_score, bet=self.bet), discord.Color.green(), "win")
        elif p_score < d_score:
            await self.disable_all_and_end(interaction, t("casino.bj_dealer_win", d_score=d_score, p_score=p_score, bet=self.bet), discord.Color.red(), "loss")
        else:
            await self.disable_all_and_end(interaction, t("casino.bj_push"), discord.Color.gold(), "draw")

    async def on_timeout(self):
        async with self.settlement_lock:
            if self.settled:
                return
            self.settled = True
            try:
                await database.run(
                    database.resolve_interactive_wager,
                    self.wager_id, self.user.id, loss_inc=1, outcome="timeout",
                )
            except database.DatabaseOperationError:
                self.settled = False
                return
        for child in self.children: child.disabled = True
        try:
            embed = self.build_embed(game_over=False, result_msg=t("casino.bj_timeout"), color=discord.Color.dark_grey())
            await self.message.edit(embed=embed, view=self)
        except:
            pass

async def start_dice_game(ctx_or_int, bet_input):
    user = ctx_or_int.user if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.author
    bal = await database.run(database.get_user_balance, user.id)
    amount = bal if str(bet_input).lower() == "all" else int(bet_input)

    first_roll, second_roll, bot_roll = (
        RNG.randint(1, 6), RNG.randint(1, 6), RNG.randint(1, 6)
    )
    guild = ctx_or_int.guild
    result = await database.run_write(
        database.resolve_dice_wager, guild.id, user.id, amount,
        first_roll, second_roll, bot_roll,
    )
    if result is None:
        bal = await database.run(database.get_user_balance, user.id)
        msg = t("casino.err_invalid_bet_bal", bal=bal)
        return await (ctx_or_int.response.send_message(msg, ephemeral=True) if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.send(msg, ephemeral=True))
    player_roll, bot_roll = result["player_roll"], result["bot_roll"]

    if result["outcome"] == "win":
        title, desc, color = t("casino.dice_win_title"), t("casino.dice_win_desc", p_roll=player_roll, b_roll=bot_roll, amount=amount), discord.Color.green()
    elif result["outcome"] == "loss":
        title, desc, color = t("casino.dice_loss_title"), t("casino.dice_loss_desc", p_roll=player_roll, b_roll=bot_roll, amount=amount), discord.Color.red()
    else:
        title, desc, color = t("casino.dice_tie_title"), t("casino.dice_tie_desc", p_roll=player_roll), discord.Color.gold()
    new_bal, _, _, _, _ = await apply_database_result(user, result)

    embed = discord.Embed(title=title, description=desc, color=color)
    if result["loaded_die"]:
        embed.description += t(
            "casino.dice_loaded_die",
            first=result["first_roll"], second=result["second_roll"],
            kept=player_roll,
        )
    embed.set_footer(text=t("casino.dice_footer", amount=amount, bal=new_bal))
    view = PlayAgainView(user, bet_input, start_dice_game)
    
    if isinstance(ctx_or_int, discord.Interaction):
        await ctx_or_int.response.edit_message(embed=embed, view=view)
    else:
        await ctx_or_int.send(embed=embed, view=view)

async def start_roulette_game(ctx_or_int, bet, choice):
    user = ctx_or_int.user if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.author
    bet = int(bet)
    choice = choice.casefold()
    localized_colors = {
        t("casino.roulette_color_red").casefold(): "red",
        t("casino.roulette_color_black").casefold(): "black",
        t("casino.roulette_color_green").casefold(): "green",
    }
    selected_color = localized_colors.get(choice)
    if selected_color is None and not (
        choice.isdigit() and 0 <= int(choice) <= 36
    ):
        msg = t("casino.roulette_err_choice")
        return await (ctx_or_int.response.send_message(msg, ephemeral=True) if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.send(msg, ephemeral=True))
    result_num = RNG.randint(0, 36)
    red_nums = [1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36]
    
    if result_num == 0: result_color, emoji = "green", "🟢"
    elif result_num in red_nums: result_color, emoji = "red", "🔴"
    else: result_color, emoji = "black", "⚫"

    won, payout = False, 0
    if selected_color is not None:
        if selected_color == result_color:
            won, payout = True, bet * (14 if selected_color == "green" else 1)
    elif choice.isdigit():
        if int(choice) == result_num:
            won, payout = True, bet * 35 
    if won:
        result = await database.run(database.resolve_instant_wager, user.id, bet, credit=bet + payout, win_inc=1)
        title, desc, color = t("casino.roulette_win_title"), t("casino.roulette_win_desc", emoji=emoji, num=result_num, color=t(f"casino.roulette_color_{result_color}").upper(), payout=payout), discord.Color.green()
    else:
        result = await database.run(database.resolve_instant_wager, user.id, bet, loss_inc=1)
        title, desc, color = t("casino.roulette_loss_title"), t("casino.roulette_loss_desc", emoji=emoji, num=result_num, color=t(f"casino.roulette_color_{result_color}").upper(), bet=bet), discord.Color.red()
    if result is None:
        bal = await database.run(database.get_user_balance, user.id)
        msg = t("casino.err_invalid_bet_bal", bal=bal)
        return await (ctx_or_int.response.send_message(msg, ephemeral=True) if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.send(msg, ephemeral=True))
    new_bal, _, _, _, _ = await apply_database_result(user, result)

    embed = discord.Embed(title=title, description=desc, color=color)
    embed.set_footer(text=t("casino.roulette_footer", bet=bet, choice=choice, bal=new_bal))
    
    view = PlayAgainView(user, bet, start_roulette_game, choice)
    if isinstance(ctx_or_int, discord.Interaction):
        await ctx_or_int.response.edit_message(embed=embed, view=view)
    else:
        await ctx_or_int.send(embed=embed, view=view)

async def start_slots_game(ctx_or_int, bet):
    user = ctx_or_int.user if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.author
    
    now = time.time()
    if user.id in slots_cooldowns:
        time_left = slots_cooldowns[user.id] + 10 - now
        if time_left > 0:
            msg = t("casino.slots_spam", time=int(time_left))
            return await (ctx_or_int.response.send_message(msg, ephemeral=True) if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.send(msg, ephemeral=True))
    
    slots_cooldowns[user.id] = now
    bet = int(bet)
    symbols = ["🍒", "🍋", "🍇", "💎", "7️⃣", "🔔", "🍉", "⭐", "🍊"]
    s1, s2, s3 = RNG.choice(symbols), RNG.choice(symbols), RNG.choice(symbols)

    if s1 == s2 == s3:
        winnings = bet * (50 if s1 == "7️⃣" else 10)
        result = await database.run(database.resolve_instant_wager, user.id, bet, credit=bet + winnings, win_inc=1)
        title, desc, color = t("casino.slots_jackpot_title"), t("casino.slots_jackpot_desc", s1=s1, s2=s2, s3=s3, winnings=winnings), discord.Color.gold()
    elif s1 == s2 or s2 == s3 or s1 == s3:
        winnings = int(bet * 1.5)
        result = await database.run(database.resolve_instant_wager, user.id, bet, credit=bet + winnings, win_inc=1)
        title, desc, color = t("casino.slots_win_title"), t("casino.slots_win_desc", s1=s1, s2=s2, s3=s3, winnings=winnings), discord.Color.green()
    else:
        result = await database.run(database.resolve_instant_wager, user.id, bet, loss_inc=1)
        title, desc, color = t("casino.slots_loss_title"), t("casino.slots_loss_desc", s1=s1, s2=s2, s3=s3, bet=bet), discord.Color.dark_gray()
    if result is None:
        bal = await database.run(database.get_user_balance, user.id)
        msg = t("casino.err_invalid_bet_bal", bal=bal)
        return await (ctx_or_int.response.send_message(msg, ephemeral=True) if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.send(msg, ephemeral=True))
    new_bal, _, _, _, _ = await apply_database_result(user, result)

    embed = discord.Embed(title=title, description=desc, color=color)
    embed.set_footer(text=t("casino.slots_footer", bet=bet, bal=new_bal))
    
    view = PlayAgainView(user, bet, start_slots_game)
    if isinstance(ctx_or_int, discord.Interaction):
        await ctx_or_int.response.edit_message(embed=embed, view=view)
    else:
        await ctx_or_int.send(embed=embed, view=view)

class MineButton(discord.ui.Button):
    def __init__(self, x, y, is_mine):
        super().__init__(style=discord.ButtonStyle.secondary, label="\u200b", row=y) 
        self.x = x
        self.y = y
        self.is_mine = is_mine
        self.is_revealed = False

    async def callback(self, interaction: discord.Interaction):
        await self.view.handle_click(self, interaction)

class MinesweeperView(discord.ui.View):
    def __init__(self, user, bet, start_func, wager_id):
        super().__init__(timeout=120.0)
        self.user = user
        self.bet = bet
        self.start_func = start_func
        self.wager_id = wager_id
        self.clicks = 0
        self.mines_count = 3
        self.total_tiles = 20
        self.settled = False
        self.action_lock = asyncio.Lock()
        self.settlement_lock = asyncio.Lock()

        self.is_mine_list = [True] * self.mines_count + [False] * (self.total_tiles - self.mines_count)
        RNG.shuffle(self.is_mine_list)

        self.tiles = []
        for i in range(self.total_tiles):
            y = i // 5  
            x = i % 5   
            btn = MineButton(x, y, self.is_mine_list[i])
            self.tiles.append(btn)
            self.add_item(btn)

        self.cashout_btn = discord.ui.Button(style=discord.ButtonStyle.success, label=t("casino.btn_cashout"), row=4, disabled=True, emoji="💰")
        self.cashout_btn.callback = self.cashout_callback
        self.add_item(self.cashout_btn)

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "casino_mines")

    def get_multiplier(self):
        if self.clicks == 0: return 1.0
        mult = 0.98 
        for i in range(self.clicks):
            mult *= (self.total_tiles - i) / (self.total_tiles - self.mines_count - i)
        return round(mult, 2)

    async def handle_click(self, button: MineButton, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        async with self.action_lock:
            if self.settled or button.is_revealed:
                return

            button.is_revealed = True
            button.disabled = True

            if button.is_mine:
                button.style = discord.ButtonStyle.danger
                button.emoji = "💥"
                button.label = None
                await self.game_over(interaction, win=False)
            else:
                self.clicks += 1
                button.style = discord.ButtonStyle.primary
                button.emoji = "💎"
                button.label = None

                mult = self.get_multiplier()
                self.cashout_btn.label = t("casino.btn_cashout_mult", mult=mult)
                self.cashout_btn.disabled = False

                if self.clicks == (self.total_tiles - self.mines_count):
                    await self.game_over(interaction, win=True)
                else:
                    embed = self.build_embed()
                    await interaction.response.edit_message(embed=embed, view=self)

    async def cashout_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        async with self.action_lock:
            if self.settled:
                return
            await self.game_over(interaction, win=True)

    def reveal_all(self):
        for child in self.children:
            if isinstance(child, MineButton):
                child.disabled = True
                if not child.is_revealed:
                    if child.is_mine:
                        child.emoji = "💣"
                        child.style = discord.ButtonStyle.danger
                    else:
                        child.emoji = "💎"
                        child.style = discord.ButtonStyle.secondary
        self.cashout_btn.disabled = True

    async def game_over(self, interaction, win):
        async with self.settlement_lock:
            if self.settled:
                return
            self.settled = True
            self.reveal_all()
            mult = self.get_multiplier() if win else 0
            try:
                if win:
                    winnings = int(self.bet * mult)
                    result = await database.run(
                        database.resolve_interactive_wager,
                        self.wager_id, self.user.id, credit=winnings, win_inc=1,
                        outcome="cashout",
                    )
                    color = discord.Color.green()
                    title = t("casino.mines_win_cashout") if self.clicks < 17 else t("casino.mines_win_clear")
                    desc = t("casino.mines_win_desc", clicks=self.clicks, mult=mult, winnings=winnings)
                else:
                    result = await database.run(
                        database.resolve_interactive_wager,
                        self.wager_id, self.user.id, loss_inc=1, outcome="mine",
                    )
                    color = discord.Color.red()
                    title = t("casino.mines_loss_title")
                    desc = t("casino.mines_loss_desc", clicks=self.clicks, bet=self.bet)
            except Exception:
                self.settled = False
                raise

        if result is None:
            # The wager left "pending" elsewhere, typically a startup refund
            # after a lost view. Report it instead of unpacking a missing row.
            return await interaction.response.send_message(
                t("casino.err_round_already_settled"), ephemeral=True
            )

        new_bal, _, _, _, _ = await apply_database_result(self.user, result)

        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_footer(text=t("casino.mines_footer", bal=new_bal))

        play_again_view = PlayAgainView(self.user, self.bet, self.start_func)
        await interaction.response.edit_message(embed=embed, view=play_again_view)

    async def on_timeout(self):
        async with self.settlement_lock:
            if self.settled:
                return
            self.settled = True
            try:
                await database.run(
                    database.resolve_interactive_wager,
                    self.wager_id, self.user.id, loss_inc=1, outcome="timeout",
                )
            except database.DatabaseOperationError:
                self.settled = False
                return
        self.reveal_all()
        try:
            if hasattr(self, "message") and self.message:
                await self.message.edit(view=self)
        except discord.HTTPException:
            pass

    def build_embed(self):
        mult = self.get_multiplier()
        next_mult = 0.98
        for i in range(self.clicks + 1):
            next_mult *= (self.total_tiles - i) / (self.total_tiles - self.mines_count - i)
        next_mult = round(next_mult, 2)
        
        embed = discord.Embed(title=t("casino.mines_embed_title"), color=discord.Color.blue())
        embed.description = t("casino.mines_embed_desc", mult=mult, next_mult=next_mult)
        embed.add_field(name=t("casino.mines_field_bet"), value=f"{self.bet}{currency_emoji()}")
        embed.add_field(
            name=t("casino.mines_field_mines"),
            value=t("casino.count_value", count=self.mines_count),
        )
        return embed

async def start_mines_game(ctx_or_int, bet):
    user = ctx_or_int.user if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.author
    guild = ctx_or_int.guild
    bet = int(bet)
    wager_id = secrets.token_urlsafe(24)
    remaining_balance = await database.run(
        database.begin_interactive_wager,
        wager_id, guild.id, user.id, "mines", bet,
    )
    if remaining_balance is None:
        bal = await database.run(database.get_user_balance, user.id)
        msg = t("casino.err_invalid_bet_bal", bal=bal)
        return await (ctx_or_int.response.send_message(msg, ephemeral=True) if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.send(msg, ephemeral=True))

    view = MinesweeperView(user, bet, start_mines_game, wager_id)
    embed = view.build_embed()

    try:
        if isinstance(ctx_or_int, discord.Interaction):
            await ctx_or_int.response.edit_message(embed=embed, view=view)
            view.message = await ctx_or_int.original_response()
        else:
            view.message = await ctx_or_int.send(embed=embed, view=view)
    except Exception:
        await database.run(
            database.refund_interactive_wager, wager_id, "delivery_failed"
        )
        raise

class FreePlayAgainView(discord.ui.View):
    def __init__(self, user, start_func):
        super().__init__(timeout=120.0)
        self.user = user
        self.start_func = start_func
        
        btn = discord.ui.Button(label=t("casino.btn_play_again_free"), style=discord.ButtonStyle.primary, emoji="🔄")
        btn.callback = self.btn_play_again
        self.add_item(btn)

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "casino_mines")

    async def btn_play_again(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        await self.start_func(interaction)

class FreeMinesweeperView(discord.ui.View):
    def __init__(self, user, start_func):
        super().__init__(timeout=120.0)
        self.user = user
        self.start_func = start_func
        self.clicks = 0
        self.mines_count = 3
        self.total_tiles = 20

        self.is_mine_list = [True] * self.mines_count + [False] * (self.total_tiles - self.mines_count)
        RNG.shuffle(self.is_mine_list)

        for i in range(self.total_tiles):
            y = i // 5  
            x = i % 5   
            btn = MineButton(x, y, self.is_mine_list[i])
            self.add_item(btn)

        self.cashout_btn = discord.ui.Button(style=discord.ButtonStyle.success, label=t("casino.btn_cashout_free"), row=4, disabled=True, emoji="🛑")
        self.cashout_btn.callback = self.cashout_callback
        self.add_item(self.cashout_btn)

    async def interaction_check(self, interaction: discord.Interaction):
        return await require_interaction_feature(interaction, "casino_mines")

    async def handle_click(self, button: MineButton, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)

        if button.is_revealed: return

        button.is_revealed = True
        button.disabled = True

        if button.is_mine:
            button.style = discord.ButtonStyle.danger
            button.emoji = "💥"
            button.label = None
            await self.game_over(interaction, win=False)
        else:
            self.clicks += 1
            button.style = discord.ButtonStyle.primary
            button.emoji = "💎"
            button.label = None
            
            self.cashout_btn.label = t("casino.btn_cashout_free_mult", clicks=self.clicks)
            self.cashout_btn.disabled = False 
            
            if self.clicks == (self.total_tiles - self.mines_count):
                await self.game_over(interaction, win=True)
            else:
                embed = self.build_embed()
                await interaction.response.edit_message(embed=embed, view=self)

    async def cashout_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user.id:
            return await interaction.response.send_message(t("casino.err_not_your_game"), ephemeral=True)
        await self.game_over(interaction, win=True)

    def reveal_all(self):
        for child in self.children:
            if isinstance(child, MineButton):
                child.disabled = True
                if not child.is_revealed:
                    if child.is_mine:
                        child.emoji = "💣"
                        child.style = discord.ButtonStyle.danger
                    else:
                        child.emoji = "💎"
                        child.style = discord.ButtonStyle.secondary
        self.cashout_btn.disabled = True

    async def game_over(self, interaction, win):
        self.reveal_all()
        
        if win:
            color = discord.Color.green()
            title = t("casino.free_mines_win_stop") if self.clicks < 17 else t("casino.free_mines_win_clear")
            desc = t("casino.free_mines_win_desc", clicks=self.clicks)
        else:
            color = discord.Color.red()
            title = t("casino.free_mines_loss_title")
            desc = t("casino.free_mines_loss_desc", clicks=self.clicks)

        embed = discord.Embed(title=title, description=desc, color=color)
        embed.set_footer(text=t("casino.free_mines_footer"))

        play_again_view = FreePlayAgainView(self.user, self.start_func)
        await interaction.response.edit_message(embed=embed, view=play_again_view)

    def build_embed(self):
        embed = discord.Embed(title=t("casino.free_mines_embed_title"), color=discord.Color.light_grey())
        embed.description = t("casino.free_mines_embed_desc")
        embed.add_field(
            name=t("casino.free_mines_field_hits"),
            value=t("casino.bold_count_value", count=self.clicks),
        )
        embed.add_field(
            name=t("casino.free_mines_field_mines"),
            value=t("casino.count_value", count=self.mines_count),
        )
        return embed

async def start_free_mines(ctx_or_int):
    user = ctx_or_int.user if isinstance(ctx_or_int, discord.Interaction) else ctx_or_int.author
    view = FreeMinesweeperView(user, start_free_mines)
    embed = view.build_embed()

    if isinstance(ctx_or_int, discord.Interaction):
        await ctx_or_int.response.edit_message(embed=embed, view=view)
        view.message = await ctx_or_int.original_response()
    else:
        view.message = await ctx_or_int.send(embed=embed, view=view)

class Casino(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.hybrid_command(name="bal", description=t("general.cmd_bal"))
    @is_channel("channels.economy")
    async def bal(self, ctx):
        result = await database.run(database.get_full_user_data, ctx.author.id)
    
        if result:
            balance = result[0]
            embed = discord.Embed(title=t("casino.bal_title", user=ctx.author.name), color=discord.Color.gold())
            embed.add_field(name=t("casino.bal_wallet"), value=f"{balance} *{currency_emoji()}* ", inline=True)
            await ctx.send(embed=embed)
        else:
            await ctx.send(t("casino.bal_empty"))

    @commands.hybrid_command(name="daily", description=t("general.cmd_daily"))
    @is_channel("channels.economy")
    async def daily(self, ctx):
        now = datetime.now()
        
        # Resolve rewards at claim time so administrative reward changes apply immediately.
        if is_premium(ctx.author):
            coin_rw, xp_rw = await database.run(
                database.get_reward, ctx.guild.id, "daily_premium", 10000, 50
            )
        else:
            coin_rw, xp_rw = await database.run(
                database.get_reward, ctx.guild.id, "daily_normal", 5000, 50
            )
        from feature_access import is_enabled
        if not is_enabled(ctx.guild.id, "levels"):
            xp_rw = 0

        result = await database.run(
            database.claim_timed_reward, ctx.author.id, "last_daily",
            now.isoformat(), coin_rw, xp_rw, once_per_day=True,
        )
        if not result["claimed"]:
            last_daily_time = datetime.fromisoformat(result["last_claim"])
            if last_daily_time.date() == now.date():
                midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
                time_left = midnight - now
                h, remainder = divmod(int(time_left.total_seconds()), 3600)
                m, _ = divmod(remainder, 60)
                return await ctx.send(t("casino.daily_cooldown", h=h, m=m), ephemeral=True)
        new_bal, _, _, _, _ = await apply_database_result(ctx.author, result)

        embed = discord.Embed(title=t("casino.daily_title"), description=t("casino.daily_desc", amount=coin_rw), color=0xFFD700)
        embed.set_footer(text=t("casino.daily_footer", bal=new_bal))
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="work", description=t("general.cmd_work"))
    @is_channel("channels.economy")
    async def work(self, ctx):
        """Pay one work shift, using this guild's own outcome odds and text.

        Which outcome happens, what it pays and what it says are all per-guild
        configuration now: the tier weights and payout ranges are typed settings
        and the responses come from `work_responses`, falling back per tier to
        the shipped locale lines for a guild that wrote none.
        """
        user_id = ctx.author.id
        now = datetime.now()

        settings, responses = await asyncio.gather(
            database.run_read(database.get_guild_settings, ctx.guild.id),
            database.run_read(database.get_work_responses, ctx.guild.id),
        )
        settings = work_settings(settings)
        weights = work_tier_weights(settings)
        tier = weighted_tier(weights)

        if tier == "free":
            earnings = 0
            xp_reward = settings["work_xp_free"]
        elif tier == "high":
            earnings = random_payout(settings["work_high_payout_min"],
                                     settings["work_high_payout_max"])
            xp_reward = settings["work_xp_high"]
        else:
            earnings = random_payout(settings["work_payout_min"],
                                     settings["work_payout_max"])
            xp_reward = settings["work_xp_normal"]
        if earnings and is_premium(ctx.author):
            earnings = int(earnings * 1.5)
        from feature_access import is_enabled
        if not is_enabled(ctx.guild.id, "levels"):
            xp_reward = 0

        result = await database.run(
            database.claim_timed_reward, user_id, "last_job", now.isoformat(),
            earnings, xp_reward, interval_seconds=15 * 60,
        )
        if not result["claimed"]:
            last_job_time = datetime.fromisoformat(result["last_claim"])
            time_left = (last_job_time + timedelta(minutes=15)) - now
            m, s = divmod(max(0, int(time_left.total_seconds())), 60)
            return await ctx.send(t("casino.work_cooldown", m=m, s=s), ephemeral=True)
        new_bal, _, _, _, _ = await apply_database_result(ctx.author, result)

        description = work_response_text(tier, responses, earnings)
        if tier == "free":
            embed = discord.Embed(title=t("casino.work_free_title"),
                                  description=description,
                                  color=discord.Color.magenta())
            embed.set_footer(text=t("casino.work_free_footer"))
        else:
            title, colour = (
                (t("casino.work_high_title"), discord.Color.dark_red())
                if tier == "high"
                else (t("casino.work_normal_title"), discord.Color.light_gray())
            )
            embed = discord.Embed(title=title, description=description, color=colour)
            embed.set_footer(
                text=t("casino.work_footer", bal=new_bal),
            )
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="rob", description=t("general.cmd_rob"))
    @is_channel("channels.economy")
    async def rob(self, ctx, victim: discord.Member):
        user_id = ctx.author.id
        now = datetime.now()

        if victim.id == user_id: return await ctx.send(t("casino.rob_err_self"), ephemeral=True)
        if victim.bot: return await ctx.send(t("casino.rob_err_bot"), ephemeral=True)
        base_chance = 0.75 if is_premium(ctx.author) else 0.5
        passive_def = 0.85 if is_premium(victim) else 1.0
        result = await database.run(
            database.resolve_robbery, user_id, victim.id, now.isoformat(),
            base_chance, passive_def, RNG.random(), RNG.uniform(0.02, 0.10),
            ctx.guild.id,
        )
        if not result["resolved"]:
            reason = result["reason"]
            if reason == "cooldown":
                last_rob_time = datetime.fromisoformat(result["last_claim"])
                time_left = (last_rob_time + timedelta(hours=1)) - now
                m, s = divmod(max(0, int(time_left.total_seconds())), 60)
                return await ctx.send(t("casino.rob_cooldown", m=m, s=s), ephemeral=True)
            if reason == "attacker_poor":
                return await ctx.send(t("casino.rob_err_poor_t"), ephemeral=True)
            if reason == "victim_poor":
                return await ctx.send(t("casino.rob_err_poor_v", user=victim.display_name), ephemeral=True)
            return await ctx.send(t("casino.rob_err_no_db"), ephemeral=True)

        t_new_bal, _, _, _, _ = await apply_database_result(ctx.author, result)
        amount = result["amount"]
        if result["won"]:
            embed = discord.Embed(title=t("casino.rob_win_title"), color=discord.Color.blue())
            # The victim's protected reserve stays private; only the attacker's
            # own take is public.
            embed.description = t(
                "casino.rob_win_desc", user=victim.mention, amount=amount,
            )
        else:
            embed = discord.Embed(title=t("casino.rob_loss_title"), color=discord.Color.red())
            embed.description = t("casino.rob_loss_desc", amount=amount, user=victim.mention)

        footer_text = t("casino.rob_footer", bal=t_new_bal)
        
        if result["consumed_lockpick"] or result["consumed_inventory_lockpick"]:
            footer_text += t("casino.rob_footer_broken")
        if result["consumed_glove"]:
            footer_text += t("casino.rob_footer_glove")

        embed.set_footer(text=footer_text)
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="pay", description=t("general.cmd_pay"))
    @is_channel("channels.economy")
    async def pay(self, ctx, member: discord.Member, amount: int):
        if member.id == ctx.author.id:
            return await ctx.send(t("casino.pay_err_self"), ephemeral=True)
        
        if member.bot:
            return await ctx.send(t("casino.pay_err_bot"), ephemeral=True)
        
        if amount <= 0:
            return await ctx.send(t("casino.pay_err_zero"), ephemeral=True)

        result = await database.run(database.transfer_balance, ctx.author.id, member.id, amount, sender_xp=2)
        if result is None:
            author_bal = await database.run(database.get_user_balance, ctx.author.id)
            return await ctx.send(t("casino.pay_err_poor", bal=author_bal), ephemeral=True)
        author_bal, _, _, _, _ = await apply_database_result(ctx.author, result)

        embed = discord.Embed(
            title=t("casino.pay_win_title"),
            description=t("casino.pay_win_desc", amount=amount, user=member.mention),
            color=discord.Color.green()
        )
        embed.set_footer(text=t("casino.pay_footer", bal=author_bal))
        
        await ctx.send(embed=embed)

    @commands.hybrid_command(name="bj", description=t("general.cmd_bj"))
    @is_channel("channels.economy")
    async def bj(self, ctx, bet: int):
        if bet <= 0:
            return await ctx.send(t("casino.err_bet_min"), ephemeral=True)
        await start_bj_game(ctx, bet)

    @commands.hybrid_command(name="dice", description=t("general.cmd_dice"))
    @is_channel("channels.economy")
    async def dice(self, ctx, bet: str):
        await start_dice_game(ctx, bet)

    @commands.hybrid_command(name="roulette", description=t("general.cmd_roulette"))
    @is_channel("channels.economy")
    async def roulette(self, ctx, bet: int, choice: str):
        await start_roulette_game(ctx, bet, choice)

    @commands.hybrid_command(name="slots", description=t("general.cmd_slots"))
    @is_channel("channels.economy")
    async def slots(self, ctx, bet: int):
        await start_slots_game(ctx, bet)

    @commands.hybrid_command(name="mines", description=t("general.cmd_mines"))
    @is_channel("channels.economy")
    async def mines(self, ctx, bet: int):
        await start_mines_game(ctx, bet)

    @commands.hybrid_command(name="freemines", description=t("general.cmd_freemines"))
    @is_channel("channels.economy")
    async def mines_free(self, ctx):
        await start_free_mines(ctx)

async def setup(bot):
    await bot.add_cog(Casino(bot))
