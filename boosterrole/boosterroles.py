import logging
import re
from typing import Optional

import discord
from discord.ext import tasks
from redbot.core import Config, commands

log = logging.getLogger("red.boosterroles")

HEX_RE = re.compile(r"^#?([0-9a-fA-F]{6})$")
MAX_ICON_BYTES = 256 * 1024
ALLOWED_ICON_TYPES = ("image/png", "image/jpeg", "image/webp")


def parse_color(value: str) -> Optional[discord.Colour]:
    match = HEX_RE.match(value.strip())
    if not match:
        return None
    return discord.Colour(int(match.group(1), 16))


class BoosterRoles(commands.Cog):
    """Let server boosters manage their own custom role."""

    def __init__(self, bot):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=8472910365, force_registration=True)
        self.config.register_guild(enabled=True, anchor_role=None)
        self.config.register_member(role_id=None)
        self.sweep_task.start()

    def cog_unload(self):
        self.sweep_task.cancel()

    async def red_delete_data_for_user(self, **kwargs):
        # Only stores role IDs (not personal data); nothing to hand back.
        return

    # ------------------------------------------------------------------ helpers

    async def _get_role(self, member: discord.Member) -> Optional[discord.Role]:
        conf = self.config.member(member)
        role_id = await conf.role_id()
        if not role_id:
            return None
        role = member.guild.get_role(role_id)
        if role is None:
            await conf.role_id.set(None)
        return role

    async def _delete_role(self, guild: discord.Guild, member_id: int, reason: str) -> bool:
        conf = self.config.member_from_ids(guild.id, member_id)
        role_id = await conf.role_id()
        if not role_id:
            return False
        role = guild.get_role(role_id)
        if role is not None:
            try:
                await role.delete(reason=reason)
            except discord.NotFound:
                pass
            except discord.HTTPException:
                log.warning("Could not delete role %s in %s; will retry on next sweep.", role_id, guild.id)
                return False
        await conf.role_id.set(None)
        return True

    async def _place_role(self, guild: discord.Guild, role: discord.Role):
        anchor_id = await self.config.guild(guild).anchor_role()
        anchor = guild.get_role(anchor_id) if anchor_id else None
        if anchor is None:
            return
        position = min(anchor.position, guild.me.top_role.position - 1)
        if position < 1:
            return
        try:
            await role.edit(position=position, reason="Booster role placement")
        except discord.HTTPException:
            log.warning("Could not reposition role %s in %s.", role.id, guild.id)

    async def _sweep_guild(self, guild: discord.Guild) -> int:
        removed = 0
        data = await self.config.all_members(guild)
        for member_id, values in data.items():
            if not values.get("role_id"):
                continue
            member = guild.get_member(member_id)
            if member is None or member.premium_since is None:
                if await self._delete_role(guild, member_id, "No longer boosting"):
                    removed += 1
        return removed

    async def _booster_check(self, ctx: commands.Context) -> bool:
        if not await self.config.guild(ctx.guild).enabled():
            await ctx.send("Booster roles are disabled in this server.", ephemeral=True)
            return False
        if ctx.author.premium_since is None:
            await ctx.send("Only server boosters can use this command.", ephemeral=True)
            return False
        return True

    async def _require_role(self, ctx: commands.Context) -> Optional[discord.Role]:
        role = await self._get_role(ctx.author)
        if role is None:
            await ctx.send("You don't have a booster role yet. Use `/boosterrole create` first.", ephemeral=True)
        return role

    # ---------------------------------------------------------------- listeners

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.premium_since is not None and after.premium_since is None:
            await self._delete_role(after.guild, after.id, "Stopped boosting")

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        await self._delete_role(member.guild, member.id, "Left the server")

    @tasks.loop(minutes=30)
    async def sweep_task(self):
        for guild in self.bot.guilds:
            try:
                await self._sweep_guild(guild)
            except Exception:
                log.exception("Booster role sweep failed in guild %s", guild.id)

    @sweep_task.before_loop
    async def _before_sweep(self):
        await self.bot.wait_until_red_ready()

    # ----------------------------------------------------------- user commands

    @commands.hybrid_group(name="boosterrole", aliases=["brole"])
    @commands.guild_only()
    @commands.bot_has_permissions(manage_roles=True)
    async def boosterrole(self, ctx: commands.Context):
        """Manage your custom booster role."""

    @boosterrole.command(name="create")
    async def br_create(self, ctx: commands.Context, color: str, *, name: str):
        """Create your booster role.

        color: hex code like #ff66aa
        name: what the role should be called
        """
        if not await self._booster_check(ctx):
            return
        if await self._get_role(ctx.author):
            await ctx.send("You already have a booster role. Use the other subcommands to edit it.", ephemeral=True)
            return
        colour = parse_color(color)
        if colour is None:
            await ctx.send("Please give a valid hex color, e.g. `#ff66aa`.", ephemeral=True)
            return
        name = name.strip()
        if not 1 <= len(name) <= 100:
            await ctx.send("Role names must be 1-100 characters.", ephemeral=True)
            return

        async with ctx.typing():
            try:
                role = await ctx.guild.create_role(
                    name=name, colour=colour, reason=f"Booster role for {ctx.author}"
                )
                await ctx.author.add_roles(role, reason="Booster role")
            except discord.HTTPException as e:
                await ctx.send(f"Discord refused that: `{e}`", ephemeral=True)
                return
            await self.config.member(ctx.author).role_id.set(role.id)
            await self._place_role(ctx.guild, role)
        await ctx.send(f"Created your role {role.mention}!", ephemeral=True)

    @boosterrole.command(name="color", aliases=["colour"])
    async def br_color(self, ctx: commands.Context, color: str):
        """Change your role's color (hex code like #ff66aa)."""
        if not await self._booster_check(ctx):
            return
        role = await self._require_role(ctx)
        if role is None:
            return
        colour = parse_color(color)
        if colour is None:
            await ctx.send("Please give a valid hex color, e.g. `#ff66aa`.", ephemeral=True)
            return
        try:
            await role.edit(colour=colour, reason="Booster role color change")
        except discord.HTTPException as e:
            await ctx.send(f"Discord refused that: `{e}`", ephemeral=True)
            return
        await ctx.send(f"Updated the color of {role.mention}.", ephemeral=True)

    @boosterrole.command(name="name", aliases=["rename"])
    async def br_name(self, ctx: commands.Context, *, name: str):
        """Rename your role."""
        if not await self._booster_check(ctx):
            return
        role = await self._require_role(ctx)
        if role is None:
            return
        name = name.strip()
        if not 1 <= len(name) <= 100:
            await ctx.send("Role names must be 1-100 characters.", ephemeral=True)
            return
        try:
            await role.edit(name=name, reason="Booster role rename")
        except discord.HTTPException as e:
            await ctx.send(f"Discord refused that: `{e}`", ephemeral=True)
            return
        await ctx.send(f"Renamed your role to {role.mention}.", ephemeral=True)

    @boosterrole.command(name="icon")
    async def br_icon(
        self,
        ctx: commands.Context,
        image: Optional[discord.Attachment] = None,
        emoji: Optional[str] = None,
    ):
        """Set your role icon with an image upload or a unicode emoji.

        Run with neither to remove the icon.
        """
        if not await self._booster_check(ctx):
            return
        role = await self._require_role(ctx)
        if role is None:
            return
        if "ROLE_ICONS" not in ctx.guild.features:
            await ctx.send(
                "This server can't use role icons yet (needs Boost Level 2).", ephemeral=True
            )
            return

        icon = None
        if image is not None:
            if image.content_type not in ALLOWED_ICON_TYPES:
                await ctx.send("Icons must be PNG, JPEG or WebP images.", ephemeral=True)
                return
            if image.size > MAX_ICON_BYTES:
                await ctx.send("Icon must be 256 KB or smaller.", ephemeral=True)
                return
            icon = await image.read()
        elif emoji:
            icon = emoji.strip()

        async with ctx.typing():
            try:
                await role.edit(display_icon=icon, reason="Booster role icon change")
            except (discord.HTTPException, TypeError, ValueError) as e:
                await ctx.send(f"Couldn't set that icon: `{e}`", ephemeral=True)
                return
        msg = "Removed your role icon." if icon is None else f"Updated the icon of {role.mention}."
        await ctx.send(msg, ephemeral=True)

    @boosterrole.command(name="delete", aliases=["remove"])
    async def br_delete(self, ctx: commands.Context):
        """Delete your booster role."""
        role = await self._get_role(ctx.author)
        if role is None:
            await ctx.send("You don't have a booster role.", ephemeral=True)
            return
        await self._delete_role(ctx.guild, ctx.author.id, "Deleted by owner")
        await ctx.send("Your booster role has been deleted.", ephemeral=True)

    # ---------------------------------------------------------- admin commands

    @commands.hybrid_group(name="boosterroleset", aliases=["broleset"])
    @commands.guild_only()
    @commands.admin_or_permissions(manage_guild=True)
    async def boosterroleset(self, ctx: commands.Context):
        """Configure booster roles."""

    @boosterroleset.command(name="anchor")
    async def brs_anchor(self, ctx: commands.Context, role: Optional[discord.Role] = None):
        """Set the role booster roles are placed directly under.

        Put it above any colored roles you want booster colors to override.
        Leave empty to clear.
        """
        if role is None:
            await self.config.guild(ctx.guild).anchor_role.set(None)
            await ctx.send("Cleared the anchor role. New roles will appear at the bottom.")
            return
        if role >= ctx.guild.me.top_role:
            await ctx.send("That role is above my highest role; I can't place roles under it.")
            return
        await self.config.guild(ctx.guild).anchor_role.set(role.id)
        await ctx.send(f"New booster roles will be placed under {role.mention}.")

    @boosterroleset.command(name="toggle")
    async def brs_toggle(self, ctx: commands.Context):
        """Enable or disable booster roles in this server."""
        conf = self.config.guild(ctx.guild).enabled
        new = not await conf()
        await conf.set(new)
        await ctx.send(f"Booster roles are now {'enabled' if new else 'disabled'}.")

    @boosterroleset.command(name="sweep")
    async def brs_sweep(self, ctx: commands.Context):
        """Immediately delete roles belonging to non-boosters."""
        async with ctx.typing():
            removed = await self._sweep_guild(ctx.guild)
        await ctx.send(f"Removed {removed} role(s).")
