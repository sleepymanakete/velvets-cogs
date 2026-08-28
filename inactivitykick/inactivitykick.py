"""
InactivityKick - a Red-DiscordBot cog
--------------------------------------
Handles members who haven't sent a message in a configurable number of
number of days. Supports two actions:

  kick        - removes them from the server (with a DM heads-up first)
  quarantine  - strips their roles, assigns a holding role, and moves
                them (visually) into a single holding channel. Typing
                anything in that channel instantly and automatically
                restores their original roles.

Commands (all under the `inactivity` group, require Manage Server):
    [p]inactivity setdays <n>
    [p]inactivity setaction <kick|quarantine>
    [p]inactivity setinactiverole <role>       (quarantine mode)
    [p]inactivity setholdingchannel <channel>  (quarantine mode)
    [p]inactivity toggle
    [p]inactivity exemptrole <role>
    [p]inactivity unexemptrole <role>
    [p]inactivity whitelist <member>
    [p]inactivity unwhitelist <member>
    [p]inactivity status
    [p]inactivity check
"""

import discord
from redbot.core import commands, Config, checks
from redbot.core.bot import Red
from discord.ext import tasks
from datetime import datetime, timezone, timedelta

COLOR_OK = discord.Color.green()
COLOR_INFO = discord.Color.blurple()
COLOR_WARN = discord.Color.orange()
COLOR_ERROR = discord.Color.red()


def make_embed(title, description="", color=COLOR_INFO, fields=None):
    embed = discord.Embed(title=title, description=description, color=color)
    for name, value, inline in (fields or []):
        embed.add_field(name=name, value=value, inline=inline)
    embed.set_footer(text="InactivityKick")
    return embed


class InactivityKick(commands.Cog):
    """Kicks or quarantines members who haven't been active in a configurable number of days."""

    def __init__(self, bot: Red):
        self.bot = bot
        self.config = Config.get_conf(self, identifier=0x1AC71EE5, force_registration=True)

        default_guild = {
            "days": 30,
            "enabled": False,
            "action": "kick",  # "kick" or "quarantine"
            "exempt_roles": [],
            "whitelist": [],
            "bot_first_seen": None,  # set on first run per-guild
            "inactive_role": None,
            "holding_channel": None,
        }
        default_member = {
            "last_seen": None,      # ISO timestamp, set whenever they send a message
            "stored_roles": None,   # list of role IDs saved when quarantined
        }

        self.config.register_guild(**default_guild)
        self.config.register_member(**default_member)

        self.inactivity_check.start()

    def cog_unload(self):
        self.inactivity_check.cancel()

    # ---------- listeners ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        await self.config.member(message.author).last_seen.set(
            datetime.now(timezone.utc).isoformat()
        )
        await self._maybe_restore(message)

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        # Start the clock the moment they join, so new members aren't flagged instantly
        await self.config.member(member).last_seen.set(
            datetime.now(timezone.utc).isoformat()
        )

    # ---------- quarantine restore ----------

    async def _maybe_restore(self, message: discord.Message):
        """If this message was sent in the holding channel by someone
        currently quarantined, restore their roles automatically."""
        guild = message.guild
        settings = await self.config.guild(guild).all()
        holding_channel_id = settings["holding_channel"]
        inactive_role_id = settings["inactive_role"]
        if holding_channel_id is None or inactive_role_id is None:
            return
        if message.channel.id != holding_channel_id:
            return

        member = message.author
        inactive_role = guild.get_role(inactive_role_id)
        if inactive_role is None or inactive_role not in member.roles:
            return

        stored_role_ids = await self.config.member(member).stored_roles()
        roles_to_restore = []
        if stored_role_ids:
            for rid in stored_role_ids:
                role = guild.get_role(rid)
                if role is not None:
                    roles_to_restore.append(role)

        try:
            if roles_to_restore:
                await member.add_roles(*roles_to_restore, reason="Returned from inactivity quarantine")
            await member.remove_roles(inactive_role, reason="Returned from inactivity quarantine")
        except discord.Forbidden:
            return

        await self.config.member(member).stored_roles.set(None)

    # ---------- helpers ----------

    async def _bot_first_seen(self, guild: discord.Guild) -> datetime:
        ts = await self.config.guild(guild).bot_first_seen()
        if ts is None:
            now = datetime.now(timezone.utc)
            await self.config.guild(guild).bot_first_seen.set(now.isoformat())
            return now
        return datetime.fromisoformat(ts)

    async def _get_last_seen(self, guild: discord.Guild, member: discord.Member) -> datetime:
        ts = await self.config.member(member).last_seen()
        if ts:
            return datetime.fromisoformat(ts)
        # No recorded activity since the bot has been watching -> don't trust join
        # date (it can be years old and has nothing to do with real activity).
        # Use whichever is more recent: when they joined, or when the bot started
        # tracking this guild, so nobody looks falsely ancient right after install.
        first_seen = await self._bot_first_seen(guild)
        joined = member.joined_at or first_seen
        if joined.tzinfo is None:
            joined = joined.replace(tzinfo=timezone.utc)
        return max(joined, first_seen)

    async def _is_exempt(self, member: discord.Member, settings: dict) -> bool:
        if member.bot:
            return True
        if member.guild_permissions.administrator:
            return True
        if member.id in settings["whitelist"]:
            return True
        # Already-quarantined members are exempt from being reprocessed
        if settings["inactive_role"] is not None:
            if any(r.id == settings["inactive_role"] for r in member.roles):
                return True
        exempt_role_ids = set(settings["exempt_roles"])
        return any(role.id in exempt_role_ids for role in member.roles)

    async def _find_inactive_members(self, guild: discord.Guild):
        settings = await self.config.guild(guild).all()
        cutoff = datetime.now(timezone.utc) - timedelta(days=settings["days"])
        inactive = []
        for member in guild.members:
            if await self._is_exempt(member, settings):
                continue
            last_seen = await self._get_last_seen(guild, member)
            if last_seen < cutoff:
                inactive.append((member, last_seen))
        return inactive

    # ---------- enforcement (shared by loop + manual trigger) ----------

    async def _process_guild(self, guild: discord.Guild, settings: dict = None) -> int:
        """Actually process (kick/quarantine) all currently-inactive members
        in this guild. Returns how many were processed. Assumes the caller
        has already checked action-specific setup (role/channel) if needed."""
        if settings is None:
            settings = await self.config.guild(guild).all()

        inactive = await self._find_inactive_members(guild)

        if settings["action"] == "kick":
            for member, last_seen in inactive:
                try:
                    dm_embed = make_embed(
                        f"Removed from {guild.name}",
                        f"You were kicked for inactivity — no messages in "
                        f"**{settings['days']}+ days**. Feel free to rejoin anytime!",
                        COLOR_WARN,
                    )
                    await member.send(embed=dm_embed)
                except discord.Forbidden:
                    pass
                try:
                    await guild.kick(member, reason=f"Inactive for {settings['days']}+ days")
                except discord.Forbidden:
                    pass

        elif settings["action"] == "quarantine":
            inactive_role = guild.get_role(settings["inactive_role"])
            holding_channel = guild.get_channel(settings["holding_channel"])
            if inactive_role is None or holding_channel is None:
                return 0
            for member, last_seen in inactive:
                roles_to_strip = [
                    r for r in member.roles
                    if r != guild.default_role and not r.managed and r != inactive_role
                ]
                try:
                    await self.config.member(member).stored_roles.set(
                        [r.id for r in roles_to_strip]
                    )
                    if roles_to_strip:
                        await member.remove_roles(*roles_to_strip, reason=f"Inactive for {settings['days']}+ days")
                    await member.add_roles(inactive_role, reason=f"Inactive for {settings['days']}+ days")
                except discord.Forbidden:
                    continue

        return len(inactive)

    # ---------- background loop ----------

    @tasks.loop(hours=24)
    async def inactivity_check(self):
        for guild in self.bot.guilds:
            settings = await self.config.guild(guild).all()
            if not settings["enabled"]:
                continue
            if settings["action"] == "quarantine":
                if settings["inactive_role"] is None or settings["holding_channel"] is None:
                    continue  # not fully configured, skip silently
            await self._process_guild(guild, settings)

    @inactivity_check.before_loop
    async def before_check(self):
        await self.bot.wait_until_red_ready()

    # ---------- commands ----------

    @commands.group(name="inactivity")
    @commands.guild_only()
    @checks.admin_or_permissions(manage_guild=True)
    async def inactivity(self, ctx: commands.Context):
        """Manage inactivity handling for this server."""
        # Intentionally no body here: Red automatically shows its own help
        # for a group command when no subcommand is invoked. Sending
        # anything here as well causes a duplicate reply.
        pass

    @inactivity.command(name="setdays")
    async def setdays(self, ctx: commands.Context, days: int):
        """Set how many days of silence before action is taken."""
        if days < 1:
            await ctx.send(embed=make_embed("Invalid value", "Days must be at least 1.", COLOR_ERROR))
            return
        await self.config.guild(ctx.guild).days.set(days)
        await ctx.send(embed=make_embed(
            "Threshold updated",
            f"Members will be affected after **{days} days** of inactivity.",
            COLOR_OK,
        ))

    @inactivity.command(name="setaction")
    async def setaction(self, ctx: commands.Context, action: str):
        """Set what happens to inactive members: `kick` or `quarantine`."""
        action = action.lower()
        if action not in ("kick", "quarantine"):
            await ctx.send(embed=make_embed("Invalid value", "Action must be `kick` or `quarantine`.", COLOR_ERROR))
            return
        await self.config.guild(ctx.guild).action.set(action)
        await ctx.send(embed=make_embed(
            "Action updated",
            f"Inactive members will now be **{action}ed**." if action == "kick"
            else "Inactive members will now be **quarantined** (roles stripped, moved to holding channel).",
            COLOR_OK,
        ))

    @inactivity.command(name="setinactiverole")
    async def setinactiverole(self, ctx: commands.Context, role: discord.Role):
        """Set the role assigned to quarantined members."""
        await self.config.guild(ctx.guild).inactive_role.set(role.id)
        await ctx.send(embed=make_embed(
            "Inactive role set",
            f"Quarantined members will be given **{role.name}**.",
            COLOR_OK,
        ))

    @inactivity.command(name="setholdingchannel")
    async def setholdingchannel(self, ctx: commands.Context, channel: discord.TextChannel):
        """Set the channel quarantined members are moved into. Posts a static
        explainer message in that channel (once), rather than a new message
        each time someone is quarantined."""
        await self.config.guild(ctx.guild).holding_channel.set(channel.id)
        days = await self.config.guild(ctx.guild).days()

        try:
            await channel.send(embed=make_embed(
                "You've been moved here for inactivity",
                (
                    f"You haven't posted anywhere in the server for "
                    f"**{days}+ days**, so you've been moved here to keep "
                    f"things tidy for active members. This channel is the only thing "
                    f"you can see right now.\n\n"
                    f"**Are my roles gone?**\n"
                    f"No. Every role you had is safely stored — nothing was deleted.\n\n"
                    f"**How do I get everything back?**\n"
                    f"Just type anything in this channel. Your roles are restored "
                    f"instantly and automatically — no need to ping anyone.\n\n"
                    f"*This process is fully automatic.*"
                ),
                COLOR_WARN,
            ), allowed_mentions=discord.AllowedMentions.none())
        except discord.Forbidden:
            await ctx.send(embed=make_embed(
                "Missing permissions",
                f"Holding channel set to {channel.mention}, but I couldn't post the explainer "
                f"message there — check my permissions in that channel.",
                COLOR_WARN,
            ))
            return

        await ctx.send(embed=make_embed(
            "Holding channel set",
            f"Quarantined members will be directed to {channel.mention}.",
            COLOR_OK,
        ))

    @inactivity.command(name="toggle")
    async def toggle(self, ctx: commands.Context):
        """Turn automatic inactivity handling on or off."""
        current = await self.config.guild(ctx.guild).enabled()
        new_state = not current
        if new_state:
            settings = await self.config.guild(ctx.guild).all()
            if settings["action"] == "quarantine" and (
                settings["inactive_role"] is None or settings["holding_channel"] is None
            ):
                await ctx.send(embed=make_embed(
                    "Setup incomplete",
                    "Quarantine mode needs both an inactive role and a holding channel set first. "
                    "Use `setinactiverole` and `setholdingchannel`.",
                    COLOR_ERROR,
                ))
                return
        await self.config.guild(ctx.guild).enabled.set(new_state)
        state = "enabled" if new_state else "disabled"
        await ctx.send(embed=make_embed(
            "Auto-handling toggled",
            f"Automatic inactivity handling is now **{state}**.",
            COLOR_OK if new_state else COLOR_WARN,
        ))

    @inactivity.command(name="exemptrole")
    async def exemptrole(self, ctx: commands.Context, role: discord.Role):
        """Exempt a role from inactivity handling (e.g. mods)."""
        async with self.config.guild(ctx.guild).exempt_roles() as roles:
            if role.id not in roles:
                roles.append(role.id)
        await ctx.send(embed=make_embed(
            "Role exempted",
            f"**{role.name}** is now exempt from inactivity handling.",
            COLOR_OK,
        ))

    @inactivity.command(name="unexemptrole")
    async def unexemptrole(self, ctx: commands.Context, role: discord.Role):
        """Remove a role from the exempt list."""
        async with self.config.guild(ctx.guild).exempt_roles() as roles:
            if role.id in roles:
                roles.remove(role.id)
        await ctx.send(embed=make_embed(
            "Role exemption removed",
            f"**{role.name}** is no longer exempt.",
            COLOR_WARN,
        ))

    @inactivity.command(name="whitelist")
    async def whitelist(self, ctx: commands.Context, member: discord.Member):
        """Exempt a specific person, regardless of role."""
        async with self.config.guild(ctx.guild).whitelist() as wl:
            if member.id not in wl:
                wl.append(member.id)
        await ctx.send(embed=make_embed(
            "Member whitelisted",
            f"{member.mention} won't be affected by inactivity handling.",
            COLOR_OK,
        ))

    @inactivity.command(name="unwhitelist")
    async def unwhitelist(self, ctx: commands.Context, member: discord.Member):
        """Remove a person from the whitelist."""
        async with self.config.guild(ctx.guild).whitelist() as wl:
            if member.id in wl:
                wl.remove(member.id)
        await ctx.send(embed=make_embed(
            "Whitelist updated",
            f"{member.mention} removed from the whitelist.",
            COLOR_WARN,
        ))

    @inactivity.command(name="status")
    async def status(self, ctx: commands.Context):
        """Show current inactivity-handling settings."""
        settings = await self.config.guild(ctx.guild).all()
        roles = [ctx.guild.get_role(r) for r in settings["exempt_roles"]]
        roles = [r.name for r in roles if r]
        whitelisted = [ctx.guild.get_member(u) for u in settings["whitelist"]]
        whitelisted = [m.display_name for m in whitelisted if m]

        inactive_role = ctx.guild.get_role(settings["inactive_role"]) if settings["inactive_role"] else None
        holding_channel = ctx.guild.get_channel(settings["holding_channel"]) if settings["holding_channel"] else None

        fields = [
            ("Threshold", f"{settings['days']} days", True),
            ("Action", settings["action"], True),
            ("Enabled", "✅ Yes" if settings["enabled"] else "❌ No", True),
        ]
        if settings["action"] == "quarantine":
            fields.append(("Inactive role", inactive_role.mention if inactive_role else "not set", True))
            fields.append(("Holding channel", holding_channel.mention if holding_channel else "not set", True))
        fields.append(("Exempt roles", ", ".join(roles) if roles else "none", False))
        fields.append(("Whitelisted users", ", ".join(whitelisted) if whitelisted else "none", False))

        embed = make_embed(
            "Inactivity Settings",
            color=COLOR_OK if settings["enabled"] else COLOR_INFO,
            fields=fields,
        )
        await ctx.send(embed=embed)

    @inactivity.command(name="check")
    async def check(self, ctx: commands.Context):
        """Dry run: list who WOULD be affected right now, without taking action."""
        async with ctx.typing():
            settings = await self.config.guild(ctx.guild).all()
            inactive = await self._find_inactive_members(ctx.guild)

        if not inactive:
            await ctx.send(embed=make_embed(
                "No inactive members",
                f"Everyone has been active within the last {settings['days']} days.",
                COLOR_OK,
            ))
            return

        lines = [f"{m.mention} — last seen {ls.strftime('%Y-%m-%d')}" for m, ls in inactive[:20]]
        description = "\n".join(lines)
        if len(inactive) > 20:
            description += f"\n\n...and {len(inactive) - 20} more."

        verb = "kicked" if settings["action"] == "kick" else "quarantined"
        embed = make_embed(
            f"{len(inactive)} member(s) would be {verb}",
            description,
            COLOR_WARN,
            fields=[("Threshold", f"{settings['days']} days", True)],
        )
        await ctx.send(embed=embed)

    @inactivity.command(name="runnow")
    async def runnow(self, ctx: commands.Context):
        """Immediately process everyone currently inactive — not a dry run.
        Run `[p]inactivity check` first if you want to preview who's affected."""
        settings = await self.config.guild(ctx.guild).all()

        if settings["action"] == "quarantine" and (
            settings["inactive_role"] is None or settings["holding_channel"] is None
        ):
            await ctx.send(embed=make_embed(
                "Setup incomplete",
                "Quarantine mode needs both an inactive role and a holding channel set first. "
                "Use `setinactiverole` and `setholdingchannel`.",
                COLOR_ERROR,
            ))
            return

        async with ctx.typing():
            inactive = await self._find_inactive_members(ctx.guild)

        if not inactive:
            await ctx.send(embed=make_embed(
                "No inactive members",
                f"Everyone has been active within the last {settings['days']} days. Nothing to do.",
                COLOR_OK,
            ))
            return

        verb = "kick" if settings["action"] == "kick" else "quarantine"
        await ctx.send(embed=make_embed(
            f"Processing {len(inactive)} member(s)...",
            f"About to {verb} {len(inactive)} member(s) inactive for {settings['days']}+ days. This may take a moment.",
            COLOR_WARN,
        ))

        async with ctx.typing():
            count = await self._process_guild(ctx.guild, settings)

        await ctx.send(embed=make_embed(
            "Done",
            f"Finished processing {count} member(s).",
            COLOR_OK,
        ))
