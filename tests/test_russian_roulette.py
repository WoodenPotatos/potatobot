"""Russian roulette: many members, one pot, and the money has to add up.

This is the first casino game with more than one player, and that changes what
can go wrong. Every other game has one stake and one settlement; here there are
N stakes taken at different moments, in a lobby that may never be started, and
the pot is split between everybody but one. Four properties are load-bearing.

**Every ante is its own durable wager.** That is what makes an abandoned lobby
safe: the bot does not have to remember a lobby across a restart, because
`refund_pending_wagers` finds each ante on its own at the next start.

**The house takes its edge once, off the pot** — the same rule the ladder games
follow, and the reason a survivor's share is computed from the whole pot rather
than per surviving player.

**Nothing is created.** What leaves the members' balances is the pot; what comes
back is the survivors' shares. The difference is the house edge plus the
remainder of an uneven split, and never more.

**Leaving gives the ante back in full.** The lobby is not a wager until it is
started, so a member who leaves is refunded, not charged.
"""

import os
import secrets
import tempfile
import unittest

import database
from cogs.casino import CASINO_EDGE, RUSSIAN_MAX_PLAYERS


GUILD = 909
ANTE = 1_000
START_BALANCE = 50_000


def survivor_share(ante, players):
    """The cog's arithmetic, in one place so the test cannot drift from it."""
    pot = ante * players
    return int(pot * CASINO_EDGE) // (players - 1)


class RussianRouletteMathTests(unittest.TestCase):
    def test_the_house_takes_its_edge_once_off_the_pot(self):
        """Charging per player would compound the edge with the table size."""
        for players in range(2, RUSSIAN_MAX_PLAYERS + 1):
            pot = ANTE * players
            paid_out = survivor_share(ANTE, players) * (players - 1)
            kept = pot - paid_out
            # The house keeps its 2% plus at most the rounding remainder of the
            # split — never a share that grows with the number of players.
            self.assertGreaterEqual(kept, 0)
            self.assertLess(kept, pot * (1 - CASINO_EDGE) + players)

    def test_a_survivor_makes_money_and_the_loser_loses_the_ante(self):
        for players in range(2, RUSSIAN_MAX_PLAYERS + 1):
            share = survivor_share(ANTE, players)
            self.assertGreater(share, ANTE,
                               f"{players} players: surviving must beat the ante")

    def test_a_two_player_round_is_the_worst_odds_and_still_pays(self):
        """Heads-up is a coin flip: half the ante back in expectation, minus the
        edge. It must still be a real payout rather than a rounding artefact."""
        share = survivor_share(ANTE, 2)
        self.assertEqual(int(2 * ANTE * CASINO_EDGE), share)


class RussianRouletteWagerTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "russian.db")
        database.initialize_database()
        database.register_guild(GUILD, "Roulette Guild")
        self.members = [101, 102, 103, 104]
        for member in self.members:
            self.set_balance(member, START_BALANCE)

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def set_balance(self, member, amount):
        with database.get_connection() as conn:
            conn.execute(
                "INSERT INTO users (user_id, balance) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET balance = excluded.balance",
                (member, amount))
            conn.commit()

    def balances(self):
        return {member: database.get_user_balance(member)
                for member in self.members}

    def join(self, member):
        wager_id = secrets.token_urlsafe(24)
        reservation = database.begin_interactive_wager(
            wager_id, GUILD, member, "russian", ANTE)
        self.assertIsNotNone(reservation)
        return wager_id

    def test_joining_reserves_the_ante_and_leaving_gives_it_back(self):
        before = self.balances()
        wager_id = self.join(self.members[0])
        self.assertEqual(before[self.members[0]] - ANTE,
                         database.get_user_balance(self.members[0]))
        database.refund_interactive_wager(wager_id, "left_lobby")
        self.assertEqual(before, self.balances())

    def test_an_abandoned_lobby_is_recovered_at_the_next_start(self):
        """Nobody pressed Start and the process died. Every ante comes back on
        its own, because each one is a pending wager in its own right."""
        before = self.balances()
        for member in self.members:
            self.join(member)
        self.assertTrue(all(database.get_user_balance(member) < before[member]
                            for member in self.members))
        summary = database.refund_pending_wagers("process_restart")
        self.assertEqual(len(self.members), summary["count"])
        self.assertEqual(ANTE * len(self.members), summary["amount"])
        self.assertEqual(before, self.balances())

    def test_a_resolved_round_creates_no_money(self):
        before = self.balances()
        wagers = {member: self.join(member) for member in self.members}
        loser = self.members[0]
        share = survivor_share(ANTE, len(self.members))

        for member, wager_id in wagers.items():
            lost = member == loser
            result = database.resolve_interactive_wager(
                wager_id, member, credit=0 if lost else share,
                win_inc=0 if lost else 1, loss_inc=1 if lost else 0,
                outcome="shot" if lost else "survived")
            self.assertIsNotNone(result)

        after = self.balances()
        self.assertEqual(before[loser] - ANTE, after[loser])
        for member in self.members[1:]:
            self.assertEqual(before[member] - ANTE + share, after[member])
        # Coins left the table; none arrived from nowhere.
        self.assertLess(sum(after.values()), sum(before.values()))

    def test_a_settled_ante_cannot_be_settled_twice(self):
        """A second Start, or a retry, must not pay a survivor again."""
        wager_id = self.join(self.members[0])
        share = survivor_share(ANTE, 2)
        self.assertIsNotNone(database.resolve_interactive_wager(
            wager_id, self.members[0], credit=share, win_inc=1))
        paid = database.get_user_balance(self.members[0])
        self.assertIsNone(database.resolve_interactive_wager(
            wager_id, self.members[0], credit=share, win_inc=1))
        self.assertEqual(paid, database.get_user_balance(self.members[0]))

    def test_a_member_who_cannot_afford_the_ante_is_refused(self):
        self.set_balance(self.members[0], ANTE - 1)
        self.assertIsNone(database.begin_interactive_wager(
            secrets.token_urlsafe(24), GUILD, self.members[0], "russian", ANTE))
        self.assertEqual(ANTE - 1, database.get_user_balance(self.members[0]))

    def test_a_resolved_round_does_not_refund_on_the_next_start(self):
        """Settled rows are not pending, so recovery must leave them alone."""
        wager_id = self.join(self.members[0])
        database.resolve_interactive_wager(wager_id, self.members[0],
                                           loss_inc=1, outcome="shot")
        settled = database.get_user_balance(self.members[0])
        database.refund_pending_wagers("process_restart")
        self.assertEqual(settled, database.get_user_balance(self.members[0]))


if __name__ == "__main__":
    unittest.main()
