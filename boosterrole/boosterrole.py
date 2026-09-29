import logging
from typing import Optional

import discord
from redbot.core import Config, app_commands, commands
from redbot.core.bot import Red

log = logging.getLogger("red.boosterrole")


class BoosterRole(commands.GroupCog, group_name="boosterrole", group_description="Manage your custom booster role"):
    """Custom booster role management."""

    def __init__(self, bot: Red):
        super().__init__()
        self.bot = bot
        self.config = Config.get_conf(self, identifier=892347102934, force_registration=True)

        default_guild = {
            "anchor_role_id": None,
        }
        default_member = {
            "role_id": None,
        }

        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

    # --- Helpers ---

    def _parse_color(self, hex_code: str) -> Optional[discord.Color]:
        """Convert a hex string into a discord.Color object."""
        hex_code = hex_code.strip()
        try:
            return discord.Color.from_str(hex_code)
        except ValueError:
            return None

    async def _get_or_validate_role(self, interaction: discord.Interaction) -> Optional[discord.Role]:
        """Fetch the member's custom role, clearing config if it was manually deleted."""
        role_id = await self.config.member(interaction.user).role_id()
        if not role_id:
            return None

        role = interaction.guild.get_role(role_id)
        if role is None:
            await self.config.member(interaction.user).role_id.set(None)
            return None
        return role

    async def _position_role(self, guild: discord.Guild, role: discord.Role) -> None:
        """Position the custom role right below the anchor role or the bot's top role."""
        anchor_id = await self.config.guild(guild).anchor_role_id()
        bot_top_role = guild.me.top_role

        target_pos = None
        if anchor_id:
            anchor_role = guild.get_role(anchor_id)
            if anchor_role and anchor_role < bot_top_role:
                target_pos = max(1, anchor_role.position - 1)

        if target_pos is None:
            target_pos = max(1, bot_top_role.position - 1)

        try:
            await role.edit(position=target_pos)
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning(f"Could not adjust position for role {role.id}: {e}")

    # --- Slash Commands ---

    @app_commands.command(name="create", description="Create your custom booster role.")
    @app_commands.describe(
        name="The name of your custom role",
        color="Hex color code (e.g. #FF5733 or #3498DB)",
        icon="Role icon image (PNG/JPEG, max 256KB - Server Level 2 Boost required)",
    )
    @app_commands.guild_only()
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        color: Optional[str] = None,
        icon: Optional[discord.Attachment] = None,
    ):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user

        if not isinstance(member, discord.Member):
            await interaction.followup.send("❌ This command can only be used in a server.", ephemeral=True)
            return

        # Boost check (admins bypass for testing)
        if not (member.premium_since or member.guild_permissions.administrator):
            await interaction.followup.send("❌ You must be actively boosting this server to create a custom role.", ephemeral=True)
            return

        if not guild.me.guild_permissions.manage_roles:
            await interaction.followup.send("❌ I lack the `Manage Roles` permission to create custom roles.", ephemeral=True)
            return

        existing_role = await self._get_or_validate_role(interaction)
        if existing_role:
            await interaction.followup.send(
                f"❌ You already have a custom role: {existing_role.mention}.\n"
                f"Use `/boosterrole color`, `/boosterrole icon`, or `/boosterrole name` to modify it, or `/boosterrole delete` to remove it.",
                ephemeral=True,
            )
            return

        # Color validation
        parsed_color = discord.Color.default()
        if color:
            parsed_color = self._parse_color(color)
            if parsed_color is None:
                await interaction.followup.send("❌ Invalid color format. Use a valid hex code like `#FF5733` or `#3498DB`.", ephemeral=True)
                return

        # Icon validation
        icon_bytes = None
        if icon:
            if "ROLE_ICONS" not in guild.features:
                await interaction.followup.send("⚠️ This server hasn't unlocked Role Icons (Boost Level 2 required). Creating without an icon.", ephemeral=True)
            elif not icon.content_type or not icon.content_type.startswith("image/"):
                await interaction.followup.send("❌ The uploaded icon must be an image (PNG or JPEG).", ephemeral=True)
                return
            elif icon.size > 256 * 1024:
                await interaction.followup.send("❌ Role icon files cannot exceed 256 KB.", ephemeral=True)
                return
            else:
                try:
                    icon_bytes = await icon.read()
                except discord.HTTPException:
                    await interaction.followup.send("❌ Failed to process the image attachment.", ephemeral=True)
                    return

        # Create and assign role
        try:
            role = await guild.create_role(
                name=name,
                colour=parsed_color,
                display_icon=icon_bytes,
                reason=f"Custom booster role for {member} ({member.id})",
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ Failed to create role: {e}", ephemeral=True)
            return

        await self._position_role(guild, role)

        try:
            await member.add_roles(role, reason="Booster custom role assignment")
        except discord.HTTPException as e:
            log.warning(f"Failed to assign role {role.id} to {member.id}: {e}")

        await self.config.member(member).role_id.set(role.id)

        embed = discord.Embed(
            title="Custom Booster Role Created!",
            description=f"Your role {role.mention} has been created and equipped.",
            color=role.color if role.color.value != 0 else discord.Color.blurple(),
        )
        embed.add_field(name="Name", value=role.name, inline=True)
        embed.add_field(name="Color", value=str(role.color), inline=True)
        if role.display_icon:
            embed.set_thumbnail(url=role.display_icon.url)

        await interaction.followup.send(embed=embed, ephemeral=True)

    @app_commands.command(name="color", description="Change the color of your booster role.")
    @app_commands.describe(hex_code="Hex color code (e.g. #FF5733 or #3498DB)")
    @app_commands.guild_only()
    async def color(self, interaction: discord.Interaction, hex_code: str):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user

        if not (member.premium_since or member.guild_permissions.administrator):
            await interaction.followup.send("❌ You must be actively boosting this server.", ephemeral=True)
            return

        role = await self._get_or_validate_role(interaction)
        if not role:
            await interaction.followup.send("❌ You don't have an active booster role. Run `/boosterrole create` first.", ephemeral=True)
            return

        parsed_color = self._parse_color(hex_code)
        if parsed_color is None:
            await interaction.followup.send("❌ Invalid color format. Use a valid hex code like `#FF5733` or `#3498DB`.", ephemeral=True)
            return

        try:
            await role.edit(colour=parsed_color, reason=f"Booster role color updated by {member} ({member.id})")
        except discord.Forbidden:
            await interaction.followup.send("❌ I lack permissions to edit this role (ensure my role is higher in the role hierarchy).", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Failed to update role color: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Updated your role {role.mention} color to `{str(role.color)}`.", ephemeral=True)

    @app_commands.command(name="icon", description="Set or update your booster role icon.")
    @app_commands.describe(icon="Image file (PNG or JPEG, max 256KB)")
    @app_commands.guild_only()
    async def icon(self, interaction: discord.Interaction, icon: discord.Attachment):
        await interaction.response.defer(ephemeral=True)
        guild = interaction.guild
        member = interaction.user

        if not (member.premium_since or member.guild_permissions.administrator):
            await interaction.followup.send("❌ You must be actively boosting this server.", ephemeral=True)
            return

        if "ROLE_ICONS" not in guild.features:
            await interaction.followup.send("❌ This server has not unlocked Role Icons (Server Boost Level 2 required).", ephemeral=True)
            return

        role = await self._get_or_validate_role(interaction)
        if not role:
            await interaction.followup.send("❌ You don't have an active booster role. Run `/boosterrole create` first.", ephemeral=True)
            return

        if not icon.content_type or not icon.content_type.startswith("image/"):
            await interaction.followup.send("❌ File must be a PNG or JPEG image.", ephemeral=True)
            return

        if icon.size > 256 * 1024:
            await interaction.followup.send("❌ File size cannot exceed 256 KB.", ephemeral=True)
            return

        try:
            icon_bytes = await icon.read()
            await role.edit(display_icon=icon_bytes, reason=f"Booster role icon updated by {member} ({member.id})")
        except discord.Forbidden:
            await interaction.followup.send("❌ I lack permissions to edit this role.", ephemeral=True)
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"❌ Failed to update role icon: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Updated icon for your role {role.mention}!", ephemeral=True)

    @app_commands.command(name="remove_icon", description="Remove the icon from your booster role.")
    @app_commands.guild_only()
    async def remove_icon(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user

        if not (member.premium_since or member.guild_permissions.administrator):
            await interaction.followup.send("❌ You must be actively boosting this server.", ephemeral=True)
            return

        role = await self._get_or_validate_role(interaction)
        if not role:
            await interaction.followup.send("❌ You don't have an active booster role.", ephemeral=True)
            return

        try:
            await role.edit(display_icon=None, reason=f"Booster role icon removed by {member} ({member.id})")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ Failed to remove role icon: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Removed the icon from your role {role.mention}.", ephemeral=True)

    @app_commands.command(name="name", description="Change the name of your booster role.")
    @app_commands.describe(new_name="The new name for your custom role")
    @app_commands.guild_only()
    async def rename(self, interaction: discord.Interaction, new_name: str):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user

        if not (member.premium_since or member.guild_permissions.administrator):
            await interaction.followup.send("❌ You must be actively boosting this server.", ephemeral=True)
            return

        role = await self._get_or_validate_role(interaction)
        if not role:
            await interaction.followup.send("❌ You don't have an active booster role. Run `/boosterrole create` first.", ephemeral=True)
            return

        try:
            await role.edit(name=new_name, reason=f"Booster role name updated by {member} ({member.id})")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ Failed to update role name: {e}", ephemeral=True)
            return

        await interaction.followup.send(f"✅ Renamed your role to **{new_name}**.", ephemeral=True)

    @app_commands.command(name="delete", description="Delete your custom booster role.")
    @app_commands.guild_only()
    async def delete(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user

        role = await self._get_or_validate_role(interaction)
        if not role:
            await interaction.followup.send("❌ You do not have an active booster role.", ephemeral=True)
            return

        try:
            await role.delete(reason=f"Booster role deleted by owner {member} ({member.id})")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ Failed to delete role: {e}", ephemeral=True)
            return

        await self.config.member(member).role_id.set(None)
        await interaction.followup.send("✅ Your custom booster role has been deleted.", ephemeral=True)

    @app_commands.command(name="setanchor", description="[Admin] Anchor booster roles below a specific role in hierarchy.")
    @app_commands.describe(role="The role to anchor booster roles under (leave empty to reset to below bot's top role)")
    @app_commands.default_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def set_anchor(self, interaction: discord.Interaction, role: Optional[discord.Role] = None):
        if not interaction.user.guild_permissions.manage_roles:
            await interaction.response.send_message("❌ You require `Manage Roles` permissions to configure this.", ephemeral=True)
            return

        if role:
            await self.config.guild(interaction.guild).anchor_role_id.set(role.id)
            await interaction.response.send_message(f"✅ Booster roles will now be placed under {role.mention}.", ephemeral=True)
        else:
            await self.config.guild(interaction.guild).anchor_role_id.set(None)
            await interaction.response.send_message("✅ Anchor role reset. Booster roles will default to just below the bot's top role.", ephemeral=True)

    # --- Listeners (Auto-Cleanup) ---

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        """Automatically delete custom roles when booster status expires."""
        if before.premium_since is not None and after.premium_since is None:
            role_id = await self.config.member(after).role_id()
            if not role_id:
                return

            role = after.guild.get_role(role_id)
            if role:
                try:
                    await role.delete(reason=f"{after} ({after.id}) stopped boosting the server.")
                    log.info(f"Deleted custom booster role '{role.name}' ({role.id}) for former booster {after}.")
                except (discord.Forbidden, discord.HTTPException) as e:
                    log.error(f"Failed to delete booster role {role_id} for {after.id}: {e}")

            await self.config.member(after).role_id.set(None)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        """Delete custom booster role if the member leaves the server."""
        role_id = await self.config.member(member).role_id()
        if not role_id:
            return

        role = member.guild.get_role(role_id)
        if role:
            try:
                await role.delete(reason=f"Booster {member} ({member.id}) left the server.")
                log.info(f"Deleted custom booster role '{role.name}' ({role.id}) because member left.")
            except (discord.Forbidden, discord.HTTPException) as e:
                log.error(f"Failed to delete booster role {role_id} for departing member {member.id}: {e}")

        await self.config.member(member).role_id.set(None)