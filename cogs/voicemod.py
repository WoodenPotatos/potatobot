import discord
import logging
import os
import sys
import time

# Resolve repository imports independently of the process working directory.
COG_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(COG_DIR)
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

import database
from feature_access import is_enabled, require_interaction_feature
from discord.ext import commands

from cogs.utils import BoundedCooldownMap, t, config

voice_logger = logging.getLogger("PotatoBot.VoiceMod")

voice_interaction_times = BoundedCooldownMap()


async def owns_current_channel(channel, member):
    voice = getattr(member, "voice", None)
    return (
        channel is not None
        and await database.run(database.get_active_channel_owner, channel.id) == member.id
        and voice is not None
        and voice.channel is not None
        and voice.channel.id == channel.id
    )

class LimitModal(discord.ui.Modal):
    def __init__(self, channel, user):
        super().__init__(title=t("voicemod.limit_modal_title"))
        self.limit = discord.ui.TextInput(
            label=t("voicemod.limit_label"), 
            placeholder=t("voicemod.limit_placeholder"), 
            min_length=1, max_length=2
        )
        self.add_item(self.limit)
        self.channel = channel
        self.user = user

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "temporary_voice"):
            return
        if not await owns_current_channel(self.channel, interaction.user):
            return await interaction.response.send_message(
                t("voicemod.not_your_room"), ephemeral=True
            )
        try:
            new_limit = int(self.limit.value)
            if 0 <= new_limit <= 99:
                await self.channel.edit(user_limit=new_limit)
                await database.run(
                    database.set_voice_limit, interaction.guild_id, self.user.id, new_limit
                )
                await interaction.response.send_message(t("voicemod.limit_success", limit=new_limit), ephemeral=True)
            else:
                await interaction.response.send_message(t("voicemod.limit_range_error"), ephemeral=True)
        except ValueError:
            await interaction.response.send_message(t("voicemod.limit_format_error"), ephemeral=True)

class RenameModal(discord.ui.Modal):
    def __init__(self, channel, user):
        super().__init__(title=t("voicemod.rename_modal_title"))
        self.new_name = discord.ui.TextInput(
            label=t("voicemod.rename_label"), 
            placeholder=t("voicemod.rename_placeholder"), 
            min_length=1, max_length=50
        )
        self.add_item(self.new_name)
        self.channel = channel
        self.user = user

    async def on_submit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "temporary_voice"):
            return
        if not await owns_current_channel(self.channel, interaction.user):
            return await interaction.response.send_message(
                t("voicemod.not_your_room"), ephemeral=True
            )
        name = self.new_name.value
        await self.channel.edit(name=name)
        await database.run(
            database.set_voice_name, interaction.guild_id, self.user.id, name
        )
        await interaction.response.send_message(t("voicemod.rename_success", name=name), ephemeral=True)

class BitrateSelect(discord.ui.Select):
    def __init__(self, channel, user, guild_limit):
        options = [
            discord.SelectOption(label=t("voicemod.bitrate_minimum", bitrate=8), value="8000"),
            discord.SelectOption(label=t("voicemod.bitrate_low", bitrate=32), value="32000"),
            discord.SelectOption(label=t("voicemod.bitrate_standard", bitrate=64), value="64000"),
        ]
        if guild_limit >= 96000: options.append(discord.SelectOption(label=t("voicemod.bitrate_high", bitrate=96), value="96000"))
        if guild_limit >= 128000: options.append(discord.SelectOption(label=t("voicemod.bitrate_ultra", bitrate=128), value="128000", emoji="💎"))
        if guild_limit >= 256000: options.append(discord.SelectOption(label=t("voicemod.bitrate_studio", bitrate=256), value="256000", emoji="🎙️"))
        if guild_limit >= 384000: options.append(discord.SelectOption(label=t("voicemod.bitrate_maximum", bitrate=384), value="384000", emoji="🚀"))

        super().__init__(placeholder=t("voicemod.bitrate_placeholder"), min_values=1, max_values=1, options=options)
        self.channel = channel
        self.user = user

    async def callback(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "temporary_voice"):
            return
        if interaction.user.id != self.user.id or not await owns_current_channel(
            self.channel, interaction.user
        ):
            return await interaction.response.send_message(t("voicemod.not_your_menu"), ephemeral=True)
        
        bitrate = int(self.values[0])
        await self.channel.edit(bitrate=bitrate)
        await database.run(
            database.set_voice_bitrate, interaction.guild_id, self.user.id, bitrate
        )
        await interaction.response.send_message(t("voicemod.bitrate_success", bitrate=bitrate//1000), ephemeral=True)

class BitrateView(discord.ui.View):
    def __init__(self, channel, user, guild_limit):
        super().__init__()
        self.add_item(BitrateSelect(channel, user, guild_limit))

class UserActionView(discord.ui.View):
    def __init__(self, channel, target_member, owner_id):
        super().__init__()
        self.channel = channel
        self.target = target_member
        self.owner_id = owner_id

        # Buttons must be created dynamically to use translated labels
        btn_permit = discord.ui.Button(label=t("voicemod.permit_btn"), style=discord.ButtonStyle.success, emoji="✅")
        btn_permit.callback = self.permit
        self.add_item(btn_permit)

        btn_block = discord.ui.Button(label=t("voicemod.block_btn"), style=discord.ButtonStyle.danger, emoji="⛔")
        btn_block.callback = self.block
        self.add_item(btn_block)

    async def permit(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "temporary_voice"):
            return
        if interaction.user.id != self.owner_id or not await owns_current_channel(
            self.channel, interaction.user
        ):
            return await interaction.response.send_message(
                t("voicemod.not_your_room"), ephemeral=True
            )
        await self.channel.set_permissions(self.target, connect=True)
        await database.run(
            database.set_voice_permission, interaction.guild_id,
            self.owner_id, self.target.id, 1,
        )
        await interaction.response.edit_message(content=t("voicemod.permit_success", user=self.target.display_name), view=None)

    async def block(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "temporary_voice"):
            return
        if interaction.user.id != self.owner_id or not await owns_current_channel(
            self.channel, interaction.user
        ):
            return await interaction.response.send_message(
                t("voicemod.not_your_room"), ephemeral=True
            )
        await self.channel.set_permissions(self.target, connect=False)
        if self.target in self.channel.members:
            await self.target.move_to(None)
        await database.run(
            database.set_voice_permission, interaction.guild_id,
            self.owner_id, self.target.id, 0,
        )
        await interaction.response.edit_message(content=t("voicemod.block_success", user=self.target.display_name), view=None)

class UserSelect(discord.ui.UserSelect):
    def __init__(self, channel, user):
        super().__init__(placeholder=t("voicemod.user_search_placeholder"))
        self.channel = channel
        self.user = user

    async def callback(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "temporary_voice"):
            return
        if interaction.user.id != self.user.id or not await owns_current_channel(
            self.channel, interaction.user
        ):
            return await interaction.response.send_message(t("voicemod.not_your_menu"), ephemeral=True)
        
        target = self.values[0]
        if target.id == self.user.id:
             return await interaction.response.send_message(t("voicemod.cannot_block_self"), ephemeral=True)
        if target.bot:
             return await interaction.response.send_message(t("voicemod.cannot_block_bots"), ephemeral=True)

        await interaction.response.send_message(
            t("voicemod.what_to_do_with_user", user=target.display_name), 
            view=UserActionView(self.channel, target, self.user.id), 
            ephemeral=True
        )

class UserPermsView(discord.ui.View):
    def __init__(self, channel, user):
        super().__init__()
        self.add_item(UserSelect(channel, user))

class VoiceControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        
        # Translating buttons dynamically
        btn_lock = discord.ui.Button(label=t("voicemod.lock_btn"), style=discord.ButtonStyle.danger, emoji="🔒", custom_id="vc_lock")
        btn_lock.callback = self.lock; self.add_item(btn_lock)
        
        btn_unlock = discord.ui.Button(label=t("voicemod.unlock_btn"), style=discord.ButtonStyle.success, emoji="🔓", custom_id="vc_unlock")
        btn_unlock.callback = self.unlock; self.add_item(btn_unlock)
        
        btn_claim = discord.ui.Button(label=t("voicemod.claim_btn"), style=discord.ButtonStyle.primary, emoji="👑", custom_id="vc_claim")
        btn_claim.callback = self.claim_ownership; self.add_item(btn_claim)
        
        btn_limit = discord.ui.Button(label=t("voicemod.limit_btn"), style=discord.ButtonStyle.primary, emoji="👥", custom_id="vc_limit")
        btn_limit.callback = self.set_limit; self.add_item(btn_limit)

        # Visibility controls are staff-only because they affect channel discovery.
        btn_hide = discord.ui.Button(label=t("voicemod.hide_btn"), style=discord.ButtonStyle.secondary, emoji="👻", custom_id="vc_hide")
        btn_hide.callback = self.hide; self.add_item(btn_hide)

        btn_unhide = discord.ui.Button(label=t("voicemod.unhide_btn"), style=discord.ButtonStyle.success, emoji="👁️", custom_id="vc_unhide")
        btn_unhide.callback = self.unhide; self.add_item(btn_unhide)

        btn_rename = discord.ui.Button(label=t("voicemod.rename_btn"), style=discord.ButtonStyle.primary, emoji="📝", custom_id="vc_rename")
        btn_rename.callback = self.rename; self.add_item(btn_rename)
        
        btn_bitrate = discord.ui.Button(label=t("voicemod.bitrate_btn"), style=discord.ButtonStyle.primary, emoji="📶", custom_id="vc_bitrate")
        btn_bitrate.callback = self.bitrate; self.add_item(btn_bitrate)
        
        btn_users = discord.ui.Button(label=t("voicemod.users_btn"), style=discord.ButtonStyle.primary, emoji="👤", custom_id="vc_users")
        btn_users.callback = self.manage_users; self.add_item(btn_users)

    async def interaction_check(self, interaction: discord.Interaction):
        if not await require_interaction_feature(interaction, "temporary_voice"):
            return False
        now = time.monotonic()
        retry_after = 3 - (now - voice_interaction_times.get(interaction.user.id, 0))
        if retry_after > 0:
            await interaction.response.send_message(
                t("utils.command_cooldown", seconds=int(retry_after) + 1),
                ephemeral=True,
            )
            return False
        voice_interaction_times[interaction.user.id] = now
        return True

    async def lock(self, interaction: discord.Interaction):
        owner_id = await database.run(database.get_active_channel_owner, interaction.channel.id)
        if not await owns_current_channel(interaction.channel, interaction.user):
            return await interaction.response.send_message(t("voicemod.not_your_room"), ephemeral=True)
        
        # Preserve unrelated overwrite bits while denying the default role access.
        ev_ow = interaction.channel.overwrites_for(interaction.guild.default_role)
        ev_ow.update(connect=False)
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ev_ow)
        
        # The configured member role may otherwise override the default-role denial.
        member_role_id = config.get("roles", {}).get("member")
        if member_role_id:
            member_role = interaction.guild.get_role(member_role_id)
            if member_role:
                mem_ow = interaction.channel.overwrites_for(member_role)
                mem_ow.update(connect=False)
                await interaction.channel.set_permissions(member_role, overwrite=mem_ow)

        await database.run(
            database.set_voice_lock, interaction.guild_id, owner_id, 1
        )
        await interaction.response.send_message(t("voicemod.room_locked"), ephemeral=True)

    async def unlock(self, interaction: discord.Interaction):
        owner_id = await database.run(database.get_active_channel_owner, interaction.channel.id)
        if not await owns_current_channel(interaction.channel, interaction.user):
            return await interaction.response.send_message(t("voicemod.not_your_room"), ephemeral=True)
        
        # Restore inherited behavior for the default role.
        ev_ow = interaction.channel.overwrites_for(interaction.guild.default_role)
        ev_ow.update(connect=None)
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ev_ow)
        
        # Explicitly restore the configured member role's connection permission.
        member_role_id = config.get("roles", {}).get("member")
        if member_role_id:
            member_role = interaction.guild.get_role(member_role_id)
            if member_role:
                mem_ow = interaction.channel.overwrites_for(member_role)
                mem_ow.update(connect=True)
                await interaction.channel.set_permissions(member_role, overwrite=mem_ow)

        await database.run(
            database.set_voice_lock, interaction.guild_id, owner_id, 0
        )
        await interaction.response.send_message(t("voicemod.room_unlocked"), ephemeral=True)

    async def claim_ownership(self, interaction: discord.Interaction):
        channel = interaction.channel
        owner_id = await database.run(database.get_active_channel_owner, channel.id)

        # Without a persisted owner this is not a tracked temporary room, so it
        # must stay unclaimable. Treating a missing row as "free" would hand out
        # manage_channels on any channel whose provenance was lost.
        if not owner_id:
            return await interaction.response.send_message(
                t("voicemod.claim_unavailable"), ephemeral=True
            )

        owner_member = channel.guild.get_member(owner_id)

        voice = interaction.user.voice
        if not voice or not voice.channel or voice.channel.id != channel.id:
            return await interaction.response.send_message(
                t("voicemod.claim_requires_connection"), ephemeral=True
            )

        if owner_member is not None and owner_member in channel.members:
            return await interaction.response.send_message(t("voicemod.owner_still_inside", owner=owner_member.display_name), ephemeral=True)

        await database.run(database.update_active_channel_owner, channel.id, interaction.user.id)
        await channel.set_permissions(interaction.user, manage_channels=True, move_members=True, connect=True)
        
        if owner_member:
            await channel.set_permissions(owner_member, overwrite=None)

        pref = await database.run(
            database.get_voice_settings, interaction.guild_id, interaction.user.id
        )
        new_name = pref[0] if pref and pref[0] else t("voicemod.default_room_name", user=interaction.user.display_name)
        await channel.edit(name=new_name)
        await interaction.response.send_message(t("voicemod.ownership_claimed", user=interaction.user.display_name), ephemeral=False)

    async def set_limit(self, interaction: discord.Interaction):
        if not await owns_current_channel(interaction.channel, interaction.user):
            return await interaction.response.send_message(t("voicemod.not_your_room"), ephemeral=True)
        await interaction.response.send_modal(LimitModal(interaction.channel, interaction.user))

    async def rename(self, interaction: discord.Interaction):
        if not await owns_current_channel(interaction.channel, interaction.user):
            return await interaction.response.send_message(t("voicemod.not_your_room"), ephemeral=True)
        await interaction.response.send_modal(RenameModal(interaction.channel, interaction.user))

    async def bitrate(self, interaction: discord.Interaction):
        if not await owns_current_channel(interaction.channel, interaction.user):
            return await interaction.response.send_message(t("voicemod.not_your_room"), ephemeral=True)
        limit = interaction.guild.bitrate_limit
        await interaction.response.send_message(t("voicemod.choose_bitrate"), view=BitrateView(interaction.channel, interaction.user, limit), ephemeral=True)

    async def manage_users(self, interaction: discord.Interaction):
        if not await owns_current_channel(interaction.channel, interaction.user):
            return await interaction.response.send_message(t("voicemod.not_your_room"), ephemeral=True)
        await interaction.response.send_message(t("voicemod.choose_user_perms"), view=UserPermsView(interaction.channel, interaction.user), ephemeral=True)

    async def hide(self, interaction: discord.Interaction):
        # Hiding a room is restricted to current staff configuration.
        is_admin = interaction.user.guild_permissions.administrator
        staff_roles = config.get("roles", {}).get("admin", [])
        is_staff_role = any(role.id in staff_roles for role in interaction.user.roles)
        
        if not (is_admin or is_staff_role):
            return await interaction.response.send_message(t("utils.err_no_perms"), ephemeral=True)

        # Deny discovery to the default role without replacing unrelated bits.
        ev_ow = interaction.channel.overwrites_for(interaction.guild.default_role)
        ev_ow.update(view_channel=False)
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ev_ow)
        
        # Also deny the member role, which may have an explicit visibility grant.
        member_role_id = config.get("roles", {}).get("member")
        if member_role_id:
            member_role = interaction.guild.get_role(member_role_id)
            if member_role:
                mem_ow = interaction.channel.overwrites_for(member_role)
                mem_ow.update(view_channel=False)
                await interaction.channel.set_permissions(member_role, overwrite=mem_ow)

        await interaction.response.send_message(t("voicemod.room_hidden"), ephemeral=True)

    async def unhide(self, interaction: discord.Interaction):
        # Unhiding a room is restricted to current staff configuration.
        is_admin = interaction.user.guild_permissions.administrator
        staff_roles = config.get("roles", {}).get("admin", [])
        is_staff_role = any(role.id in staff_roles for role in interaction.user.roles)
        
        if not (is_admin or is_staff_role):
            return await interaction.response.send_message(t("utils.err_no_perms"), ephemeral=True)

        # Restore inherited visibility for the default role.
        ev_ow = interaction.channel.overwrites_for(interaction.guild.default_role)
        ev_ow.update(view_channel=None)
        await interaction.channel.set_permissions(interaction.guild.default_role, overwrite=ev_ow)
        
        # Restore explicit visibility for the configured member role.
        member_role_id = config.get("roles", {}).get("member")
        if member_role_id:
            member_role = interaction.guild.get_role(member_role_id)
            if member_role:
                mem_ow = interaction.channel.overwrites_for(member_role)
                mem_ow.update(view_channel=True)
                await interaction.channel.set_permissions(member_role, overwrite=mem_ow)

        await interaction.response.send_message(t("voicemod.room_unhidden"), ephemeral=True)

class VoiceMods(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.bot.add_view(VoiceControlView())

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if not is_enabled(member.guild.id, "temporary_voice"):
            return
        if after.channel and after.channel.id in config["channels"]["join_to_create"]:
            guild = member.guild
            category = after.channel.category
        
            pref = await database.run(
                database.get_voice_settings, guild.id, member.id
            )
            name = pref[0] if pref and pref[0] else t("voicemod.default_room_name", user=member.display_name)
            limit = pref[1] if pref and pref[1] else 0
            locked = pref[2] if pref and pref[2] else 0
            saved_bitrate = pref[3] if pref and pref[3] else 64000
            final_bitrate = min(saved_bitrate, guild.bitrate_limit)

            overwrites = {}
            for target, ow in after.channel.overwrites.items():
                # Never copy member-specific overwrites from the lobby; they may grant
                # unrelated administrators access to every newly created private room.
                if isinstance(target, discord.Member):
                    continue
                
                # Skip roles the bot cannot manage safely.
                if isinstance(target, discord.Role):
                    if target >= guild.me.top_role or target == guild.me:
                        continue

                # Copy only safe role overwrites such as @everyone and the member role.
                overwrites[target] = discord.PermissionOverwrite(**{k: v for k, v in ow if v is not None})
            
            # The creator receives the minimum controls required to manage the room.
            owner_ow = overwrites.get(member, discord.PermissionOverwrite())
            owner_ow.update(manage_channels=True, move_members=True, connect=True)
            overwrites[member] = owner_ow
            
            # Supplying explicit overwrites bypasses some category inheritance, so the
            # bot must receive explicit access to manage and clean up the room.
            overwrites[guild.me] = discord.PermissionOverwrite(
                view_channel=True,
                manage_channels=True,
                move_members=True,
                connect=True,
                speak=True
            )
            
            # Reapply the owner's persisted lock state during channel creation.
            if locked == 1:
                # Deny the default role while preserving its other overwrite values.
                everyone_ow = overwrites.get(guild.default_role, discord.PermissionOverwrite())
                everyone_ow.update(connect=False)
                overwrites[guild.default_role] = everyone_ow
                
                # Deny the member role without discarding unrelated explicit permissions.
                member_role_id = config.get("roles", {}).get("member")
                if member_role_id:
                    member_role = guild.get_role(member_role_id)
                    if member_role:
                        member_ow = overwrites.get(member_role, discord.PermissionOverwrite())
                        member_ow.update(connect=False)
                        overwrites[member_role] = member_ow

            # Reapply the owner's saved per-user allow and block decisions.
            user_perms = await database.run(
                database.get_voice_permissions, guild.id, member.id
            )
            for target_id, is_allowed in user_perms:
                target_member = guild.get_member(target_id)
                if target_member:
                    tgt_ow = overwrites.get(target_member, discord.PermissionOverwrite())
                    tgt_ow.update(connect=bool(is_allowed))
                    overwrites[target_member] = tgt_ow

            new_channel = await guild.create_voice_channel(
                name=name, category=category, user_limit=limit,
                bitrate=final_bitrate, overwrites=overwrites 
            )

            await database.run(
                database.add_active_channel, guild.id, new_channel.id, member.id
            )

            try:
                await member.move_to(new_channel)
            except discord.HTTPException:
                await new_channel.delete()
                await database.run(database.remove_active_channel, new_channel.id)
                return

            view = VoiceControlView()
            msg = t("voicemod.control_panel_msg", user=member.mention)
            await new_channel.send(msg, view=view)

        if before.channel:
            is_temp = await database.run(database.get_active_channel_owner, before.channel.id) is not None

            if is_temp:
                real_members = [m for m in before.channel.members if not m.bot]
                if len(real_members) == 0:
                    try:
                        await before.channel.delete()
                    except discord.HTTPException as error:
                        # Keeping the row preserves ownership provenance for a
                        # channel that still exists, so its controls stay bound
                        # to their real owner instead of becoming claimable.
                        voice_logger.warning(
                            "Failed to delete empty temporary voice channel "
                            "(channel_id=%s, error=%s)",
                            before.channel.id,
                            type(error).__name__,
                        )
                        return

                    await database.run(database.remove_active_channel, before.channel.id)

async def setup(bot):
    await bot.add_cog(VoiceMods(bot))
