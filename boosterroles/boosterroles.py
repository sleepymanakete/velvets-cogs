import discord
from redbot.core import commands, Config
from redbot.core.bot import Red
from redbot.core.utils.chat_formatting import box
from typing import Optional


class BoosterRoles(commands.Cog):
    """Let server boosters create and customize their own personal role."""

    __author__ = "Claude"
    __version__ = "1.0.0"

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=987654321123456, force_registration=True)
        default_guild = {
            "enabled": False,
            "anchor_role": None,   # role ID new booster roles are placed just below
            "member_roles": {},    # str(member_id): role_id
            "cleanup_on_unboost": True,
            "max_name_length": 100,
            "allow_icons": True,
        }
        self.config.register_guild(**default_guild)

    # ---------------------------------------------------------------- #
    # Helpers
    # ---------------------------------------------------------------- #

    @staticmethod
    def _is_booster(member: discord.Member) -> bool:
        return member.premium_since is not None

    async def _get_role(self, guild: discord.Guild, member: discord.Member) -> Optional[discord.Role]:
        data = await self.config.guild(guild).member_roles()
        role_id = data.get(str(member.id))
        if role_id is None:
            return None
        return guild.get_role(role_id)

    async def _set_role_mapping(self, guild: discord.Guild, member: discord.Member, role: Optional[discord.Role]):
        async with self.config.guild(guild).member_roles() as roles:
            if role is None:
                roles.pop(str(member.id), None)
            else:
                roles[str(member.id)] = role.id

    @staticmethod
    def _parse_color(color_str: str) -> Optional[discord.Color]:
        color_str = color_str.strip()
        try:
            return discord.Color(int(color_str.lstrip("#"), 16))
        except ValueError:
            pass
        try:
            return discord.Color.from_str(color_str)
        except Exception:
            return None

    async def _position_role(self, guild: discord.Guild, role: discord.Role):
        anchor_id = await self.config.guild(guild).anchor_role()
        anchor = guild.get_role(anchor_id) if anchor_id else None
        if not anchor:
            return
        try:
            pos = max(anchor.position - 1, 1)
            await role.edit(position=pos)
        except discord.HTTPException:
            pass

    # ---------------------------------------------------------------- #
    # Listeners
    # ---------------------------------------------------------------- #

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        # Auto-delete the role if the member stops boosting
        if before.premium_since and not after.premium_since:
            guild = after.guild
            if not await self.config.guild(guild).cleanup_on_unboost():
                return
            role = await self._get_role(guild, after)
            if role:
                try:
                    await role.delete(reason="Member stopped boosting the server.")
                except discord.HTTPException:
                    pass
                await self._set_role_mapping(guild, after, None)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        role = await self._get_role(member.guild, member)
        if role:
            try:
                await role.delete(reason="Member left the server.")
            except discord.HTTPException:
                pass
            await self._set_role_mapping(member.guild, member, None)

    # ---------------------------------------------------------------- #
    # User commands
    # ---------------------------------------------------------------- #

    @commands.hybrid_group(name="boosterrole", aliases=["brole"], invoke_without_command=True)
    @commands.guild_only()
    async def boosterrole(self, ctx: commands.Context):
        """Manage your custom server booster role."""
        await ctx.send_help()

    @boosterrole.command(name="create")
    @commands.guild_only()
    async def boosterrole_create(self, ctx: commands.Context, color: str, *, name: str):
        """Create your custom booster role.

        Example: `[p]boosterrole create #ff0099 My Cool Role`
        """
        guild = ctx.guild
        member = ctx.author

        if not await self.config.guild(guild).enabled():
            return await ctx.send("Booster roles aren't enabled on this server.")

        if not self._is_booster(member):
            return await ctx.send("This is only available to server boosters. 💎")

        if await self._get_role(guild, member):
            return await ctx.send(
                f"You already have a booster role. Use `{ctx.clean_prefix}boosterrole color` "
                f"or `{ctx.clean_prefix}boosterrole rename` to edit it."
            )

        max_len = await self.config.guild(guild).max_name_length()
        if len(name) > max_len:
            return await ctx.send(f"That name is too long (max {max_len} characters).")

        col = self._parse_color(color)
        if col is None:
            return await ctx.send("That's not a valid color. Try a hex code like `#ff0099`.")

        if not guild.me.guild_permissions.manage_roles:
            return await ctx.send("I don't have the **Manage Roles** permission.")

        try:
            role = await guild.create_role(
                name=name,
                color=col,
                reason=f"Booster role for {member} ({member.id})",
            )
            await member.add_roles(role, reason="Assigning new booster role")
            await self._position_role(guild, role)
        except discord.Forbidden:
            return await ctx.send("I don't have permission to create or assign that role.")
        except discord.HTTPException as e:
            return await ctx.send(f"Something went wrong creating the role: {e}")

        await self._set_role_mapping(guild, member, role)
        await ctx.send(
            f"Created your booster role {role.mention}! Use `{ctx.clean_prefix}boosterrole color` "
            "to change its color anytime."
        )

    @boosterrole.command(name="color", aliases=["colour"])
    @commands.guild_only()
    async def boosterrole_color(self, ctx: commands.Context, *, color: str):
        """Change the color of your booster role."""
        guild = ctx.guild
        member = ctx.author

        if not self._is_booster(member):
            return await ctx.send("This is only available to server boosters. 💎")

        role = await self._get_role(guild, member)
        if not role:
            return await ctx.send(
                f"You don't have a booster role yet. Create one with `{ctx.clean_prefix}boosterrole create`."
            )

        col = self._parse_color(color)
        if col is None:
            return await ctx.send("That's not a valid color. Try a hex code like `#ff0099`.")

        try:
            await role.edit(color=col, reason=f"Color change requested by {member}")
        except discord.Forbidden:
            return await ctx.send("I don't have permission to edit that role (check role hierarchy).")
        except discord.HTTPException as e:
            return await ctx.send(f"Something went wrong: {e}")

        await ctx.send(f"Updated {role.mention}'s color to `{str(col)}`.")

    @boosterrole.command(name="rename", aliases=["name"])
    @commands.guild_only()
    async def boosterrole_rename(self, ctx: commands.Context, *, name: str):
        """Rename your booster role."""
        guild = ctx.guild
        member = ctx.author

        if not self._is_booster(member):
            return await ctx.send("This is only available to server boosters. 💎")

        role = await self._get_role(guild, member)
        if not role:
            return await ctx.send(
                f"You don't have a booster role yet. Create one with `{ctx.clean_prefix}boosterrole create`."
            )

        max_len = await self.config.guild(guild).max_name_length()
        if len(name) > max_len:
            return await ctx.send(f"That name is too long (max {max_len} characters).")

        try:
            await role.edit(name=name, reason=f"Rename requested by {member}")
        except discord.Forbidden:
            return await ctx.send("I don't have permission to edit that role (check role hierarchy).")
        except discord.HTTPException as e:
            return await ctx.send(f"Something went wrong: {e}")

        await ctx.send(f"Renamed your booster role to **{name}**.")

    @boosterrole.command(name="icon")
    @commands.guild_only()
    async def boosterrole_icon(self, ctx: commands.Context, attachment_url: Optional[str] = None):
        """Set an icon for your booster role (needs Level 2 boost perks unlocked).

        Attach an image to your message, or provide a direct image URL.
        """
        guild = ctx.guild
        member = ctx.author

        if not await self.config.guild(guild).allow_icons():
            return await ctx.send("Role icons are disabled on this server.")

        if "ROLE_ICONS" not in guild.features:
            return await ctx.send("This server hasn't unlocked role icons (needs Level 2 boost perks).")

        if not self._is_booster(member):
            return await ctx.send("This is only available to server boosters. 💎")

        role = await self._get_role(guild, member)
        if not role:
            return await ctx.send(
                f"You don't have a booster role yet. Create one with `{ctx.clean_prefix}boosterrole create`."
            )

        image_bytes = None
        if ctx.message.attachments:
            image_bytes = await ctx.message.attachments[0].read()
        elif attachment_url:
            async with self.bot.session.get(attachment_url) as resp:
                if resp.status != 200:
                    return await ctx.send("Couldn't download that image.")
                image_bytes = await resp.read()
        else:
            return await ctx.send("Attach an image or provide a direct image URL.")

        try:
            await role.edit(display_icon=image_bytes, reason=f"Icon set by {member}")
        except discord.Forbidden:
            return await ctx.send("I don't have permission to edit that role.")
        except discord.HTTPException as e:
            return await ctx.send(f"Something went wrong: {e}")

        await ctx.send(f"Updated {role.mention}'s icon.")

    @boosterrole.command(name="delete", aliases=["remove"])
    @commands.guild_only()
    async def boosterrole_delete(self, ctx: commands.Context):
        """Delete your booster role."""
        guild = ctx.guild
        member = ctx.author

        role = await self._get_role(guild, member)
        if not role:
            return await ctx.send("You don't have a booster role.")

        try:
            await role.delete(reason=f"Deleted by {member}")
        except discord.Forbidden:
            return await ctx.send("I don't have permission to delete that role.")
        except discord.HTTPException as e:
            return await ctx.send(f"Something went wrong: {e}")

        await self._set_role_mapping(guild, member, None)
        await ctx.send("Your booster role has been deleted.")

    @boosterrole.command(name="show", aliases=["info"])
    @commands.guild_only()
    async def boosterrole_show(self, ctx: commands.Context):
        """Show info about your booster role."""
        role = await self._get_role(ctx.guild, ctx.author)
        if not role:
            return await ctx.send("You don't have a booster role yet.")
        embed = discord.Embed(title=role.name, color=role.color)
        embed.add_field(name="Color", value=str(role.color))
        embed.add_field(name="Members", value=str(len(role.members)))
        embed.add_field(name="Position", value=str(role.position))
        icon = getattr(role, "display_icon", None)
        if icon:
            icon_url = icon if isinstance(icon, str) else icon.url
            embed.set_thumbnail(url=icon_url)
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------- #
    # Admin commands
    # ---------------------------------------------------------------- #

    @boosterrole.group(name="admin")
    @commands.guild_only()
    @commands.admin_or_permissions(manage_roles=True)
    async def boosterrole_admin(self, ctx: commands.Context):
        """Admin settings for booster roles."""
        if ctx.invoked_subcommand is None:
            await ctx.send_help()

    @boosterrole_admin.command(name="toggle")
    async def admin_toggle(self, ctx: commands.Context, on_off: Optional[bool] = None):
        """Enable or disable the booster role system."""
        current = await self.config.guild(ctx.guild).enabled()
        new = on_off if on_off is not None else not current
        await self.config.guild(ctx.guild).enabled.set(new)
        await ctx.send(f"Booster roles are now {'enabled' if new else 'disabled'}.")

    @boosterrole_admin.command(name="anchor")
    async def admin_anchor(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        """Set the role new booster roles are placed just below.

        Run with no role to clear the anchor (uses default positioning).
        """
        if role is None:
            await self.config.guild(ctx.guild).anchor_role.set(None)
            return await ctx.send("Anchor role cleared. New roles will use default positioning.")
        await self.config.guild(ctx.guild).anchor_role.set(role.id)
        await ctx.send(f"New booster roles will be placed just below {role.mention}.")

    @boosterrole_admin.command(name="cleanup")
    async def admin_cleanup(self, ctx: commands.Context, on_off: Optional[bool] = None):
        """Toggle auto-deleting a member's role when they stop boosting."""
        current = await self.config.guild(ctx.guild).cleanup_on_unboost()
        new = on_off if on_off is not None else not current
        await self.config.guild(ctx.guild).cleanup_on_unboost.set(new)
        await ctx.send(f"Auto-cleanup on unboost is now {'enabled' if new else 'disabled'}.")

    @boosterrole_admin.command(name="maxlength")
    async def admin_maxlength(self, ctx: commands.Context, length: int):
        """Set the max character length allowed for role names."""
        length = max(1, min(length, 100))
        await self.config.guild(ctx.guild).max_name_length.set(length)
        await ctx.send(f"Max role name length set to {length}.")

    @boosterrole_admin.command(name="icons")
    async def admin_icons(self, ctx: commands.Context, on_off: Optional[bool] = None):
        """Toggle whether boosters are allowed to set role icons."""
        current = await self.config.guild(ctx.guild).allow_icons()
        new = on_off if on_off is not None else not current
        await self.config.guild(ctx.guild).allow_icons.set(new)
        await ctx.send(f"Role icons are now {'allowed' if new else 'disallowed'}.")

    @boosterrole_admin.command(name="list")
    async def admin_list(self, ctx: commands.Context):
        """List all active booster roles."""
        data = await self.config.guild(ctx.guild).member_roles()
        if not data:
            return await ctx.send("No booster roles have been created yet.")
        lines = []
        for member_id, role_id in data.items():
            member = ctx.guild.get_member(int(member_id))
            role = ctx.guild.get_role(role_id)
            member_str = member.mention if member else f"`{member_id}` (left)"
            role_str = role.mention if role else f"`{role_id}` (deleted)"
            lines.append(f"{member_str} -> {role_str}")
        chunks = [lines[i:i + 15] for i in range(0, len(lines), 15)]
        for i, chunk in enumerate(chunks):
            await ctx.send(f"**Booster Roles ({i + 1}/{len(chunks)})**\n{box(chr(10).join(chunk))}")

    @boosterrole_admin.command(name="purge")
    async def admin_purge(self, ctx: commands.Context):
        """Delete roles belonging to members who left or are no longer boosting."""
        guild = ctx.guild
        removed = 0
        async with self.config.guild(guild).member_roles() as roles:
            for member_id in list(roles.keys()):
                member = guild.get_member(int(member_id))
                role = guild.get_role(roles[member_id])
                if member is None or member.premium_since is None:
                    if role:
                        try:
                            await role.delete(reason="Booster role purge: left or no longer boosting")
                        except discord.HTTPException:
                            pass
                    del roles[member_id]
                    removed += 1
        await ctx.send(f"Purged {removed} booster role(s).")
