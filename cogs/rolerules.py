"""Conditional role rules: roles that follow other roles.

A guild declares rules in the `role_rules` setting (decided 2026-11-06): *A
grants B* (having any condition role gives the target roles, and losing the
last one takes them back), *B requires A* (a target role goes while the member
holds no condition role) and *exclusive groups* (a member holds one role of the
group; the newest wins). They apply to every role change, whoever made it,
including the bot's own level rewards and role menus.

The planner is a pure function of the rules and the member's roles before and
after a change. It runs the rules to a fixed point, so the bot's own edit, which
Discord reports as one more member update, plans nothing and the chain ends
there; a pass limit stops a contradictory rule set from looping instead.
"""

import asyncio
import logging
import os
import sys

import discord
from discord.ext import commands

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from cogs.utils import can_self_assign_role, guild_setting_sync, t
from core.bounded import BoundedValueMap
from core.feature_access import is_enabled, maintenance_blocks

rules_logger = logging.getLogger("PotatoBot.RoleRules")

# Enough for any consistent rule set to settle; a contradictory one (A grants
# B while B and A are exclusive) stops here rather than looping.
MAX_PASSES = 5

# One lock per (guild, member): the bot's own edit arrives as another update,
# and two plans for one member must not interleave.
member_locks = BoundedValueMap()


def _ids(values) -> set[int]:
    return {int(value) for value in values or ()}


def plan_role_changes(rules: dict, before: set[int], after: set[int]):
    """The roles to add and to remove so `after` satisfies every rule.

    Rules act on what just changed: a grant takes its roles back only when its
    last condition role *was removed now*, so a role given by hand is left
    alone, and an exclusive group keeps the role that arrived in this change.
    Returns `(to_add, to_remove)`, both possibly empty.
    """
    roles = set(after)
    added, removed = set(after) - set(before), set(before) - set(after)
    for _ in range(MAX_PASSES):
        start = set(roles)
        for rule in rules.values():
            when, then = _ids(rule.get("when_ids")), _ids(rule.get("then_ids"))
            kind = rule.get("type")
            if kind == "exclusive":
                held = roles & then
                if len(held) > 1:
                    fresh = held & added
                    keep = min(fresh) if fresh else min(held)
                    roles -= held - {keep}
            elif kind == "grants":
                if roles & when:
                    roles |= then
                elif removed & when:
                    # Taken back only if no other grant still gives it.
                    still_given = set()
                    for other in rules.values():
                        if other.get("type") == "grants" and roles & _ids(other.get("when_ids")):
                            still_given |= _ids(other.get("then_ids"))
                    roles -= then - still_given
            elif kind == "requires":
                if not roles & when:
                    roles -= then
        added |= roles - start
        removed |= start - roles
        if roles == start:
            break
    return roles - set(after), set(after) - roles


class RoleRules(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_member_update(self, before, after):
        if before.roles != after.roles:
            await self.apply_rules(after, {role.id for role in before.roles})

    @commands.Cog.listener()
    async def on_member_join(self, member):
        # A member can arrive holding roles (another bot, an onboarding flow),
        # so a join is a change from nothing.
        await self.apply_rules(member, set())

    async def apply_rules(self, member, before_ids: set[int]):
        """Plan and apply the rules for one member's role change. Never raises:
        a member update is no place for an exception to escape."""
        guild = member.guild
        if member.bot or not is_enabled(guild.id, "role_rules") \
                or maintenance_blocks(guild, member):
            return
        try:
            rules = guild_setting_sync(guild.id, "role_rules") or {}
        except Exception:
            rules_logger.exception("Role rules unreadable (guild_id=%s)", guild.id)
            return
        if not rules:
            return
        key = (guild.id, member.id)
        lock = member_locks.get(key)
        if lock is None:
            lock = member_locks[key] = asyncio.Lock()
        async with lock:
            after_ids = {role.id for role in member.roles}
            to_add, to_remove = plan_role_changes(rules, before_ids, after_ids)
            # The same refusal role menus give: never a privileged, staff,
            # managed or above-the-bot role, whatever a rule says.
            grant = [role for role in map(guild.get_role, sorted(to_add))
                     if role is not None and can_self_assign_role(guild, role)]
            take = [role for role in map(guild.get_role, sorted(to_remove))
                    if role is not None and can_self_assign_role(guild, role)]
            reason = t("rolerules.audit_reason")
            try:
                if take:
                    await member.remove_roles(*take, reason=reason)
                if grant:
                    await member.add_roles(*grant, reason=reason)
            except discord.HTTPException as error:
                rules_logger.warning(
                    "Role rule change failed (guild_id=%s, error=%s)",
                    guild.id, type(error).__name__)


async def setup(bot):
    await bot.add_cog(RoleRules(bot))
